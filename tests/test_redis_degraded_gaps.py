"""Redis 降级到内存实现时的能力缺口回归测试。

降级实现原先缺 pipeline / scan / 多键 delete，几处调用点会直接抛异常或被
try/except 吞成静默失败；ping() 恒返回 True 又让 /health 在 Redis 完全挂掉时
一片绿。
"""

import json
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import core.queue_manager as qm  # noqa: E402
from core.queue_manager import _InMemoryRedis, is_degraded  # noqa: E402


class InMemoryRedisCapabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.r = _InMemoryRedis()

    def test_delete_accepts_multiple_keys_and_returns_count(self) -> None:
        # conversation_memory 用的是 delete(*keys) 并累加返回值
        self.r.set("a", "1")
        self.r.set("b", "2")
        self.r.rpush("c", "x")

        self.assertEqual(self.r.delete("a", "b", "c", "missing"), 3)
        self.assertIsNone(self.r.get("a"))

    def test_scan_returns_matching_keys_and_terminates(self) -> None:
        self.r.set("ai:history:u1", "x")
        self.r.set("ai:history:u2", "y")
        self.r.set("music:queue", "z")

        cursor, keys = self.r.scan(0, match="ai:history:*", count=100)

        self.assertEqual(cursor, 0, "游标必须归零，否则调用方的 while 循环不退出")
        self.assertEqual(sorted(keys), ["ai:history:u1", "ai:history:u2"])

    def test_scan_then_delete_matches_the_real_call_site(self) -> None:
        for i in range(3):
            self.r.set(f"ai:history:{i}", "x")

        _cursor, keys = self.r.scan(0, match="ai:history:*", count=100)
        removed = self.r.delete(*keys)

        self.assertEqual(removed, 3)

    def test_pipeline_executes_in_order(self) -> None:
        pipe = self.r.pipeline(transaction=False)
        pipe.set("k", "v")
        pipe.get("k")
        pipe.llen("nope")

        self.assertEqual(pipe.execute(), [None, "v", 0])

    def test_pipeline_is_chainable_and_resets_after_execute(self) -> None:
        pipe = self.r.pipeline(transaction=False)
        pipe.set("a", "1").set("b", "2")
        pipe.execute()

        self.assertEqual(pipe.execute(), [], "execute 之后应清空已排队的命令")

    def test_eval_is_deliberately_absent(self) -> None:
        # 内存里不解释 Lua，原子队列操作走明确的内存后端契约。
        self.assertFalse(hasattr(self.r, "eval"))

    def test_expire_is_deliberately_absent(self) -> None:
        # web_link_token 已有 hasattr 探测，补了反而让那处探测变成死代码
        self.assertFalse(hasattr(self.r, "expire"))


class DegradedStateTest(unittest.TestCase):
    def setUp(self) -> None:
        # 这些是模块级单例，用例间必须存档还原
        self._client = qm._redis_client
        self._retry = qm._last_redis_retry
        self._generation = qm._redis_generation
        self._probe = qm._redis_probe_in_flight

    def tearDown(self) -> None:
        qm._redis_client = self._client
        qm._last_redis_retry = self._retry
        qm._redis_generation = self._generation
        qm._redis_probe_in_flight = self._probe

    def test_memory_fallback_is_reported_as_degraded(self) -> None:
        qm._redis_client = _InMemoryRedis()
        self.assertTrue(is_degraded())

    def test_real_client_is_not_degraded(self) -> None:
        qm._redis_client = object()
        self.assertFalse(is_degraded())

    def _health_redis_status(self) -> str:
        import web.web_player as web_player

        with mock.patch.object(web_player, "get_redis", return_value=_InMemoryRedis()):
            request = mock.Mock()
            with mock.patch.object(web_player, "_is_admin_authorized", return_value=True):
                body = json.loads(bytes(web_player.health_check(request).body))
        return body["checks"]["redis"]["status"]

    def test_health_reports_memory_fallback(self) -> None:
        # ping() 恒返回 True，只看 ping 的话 Redis 完全挂掉时 /health 一片绿
        qm._redis_client = _InMemoryRedis()
        self.assertEqual(self._health_redis_status(), "degraded_memory")

    def test_health_reports_ok_on_a_real_client(self) -> None:
        qm._redis_client = object()
        self.assertEqual(self._health_redis_status(), "ok")


class QueueActionAtomicityTest(unittest.TestCase):
    """内存降级后仍必须通过后端契约原子修改域队列。"""

    area = "area-A"
    key = "music:area-A:queue"

    def _queue(self):
        r = _InMemoryRedis()
        for name in ("a", "b", "c"):
            r.rpush(self.key, name)
        return r

    def test_remove_uses_memory_backend_contract(self) -> None:
        from web.web_player import execute_queue_action

        r = self._queue()
        self.assertEqual(execute_queue_action("remove", 1, r, area=self.area), {"ok": True})
        self.assertEqual(r.lrange(self.key, 0, -1), ["a", "c"])

    def test_top_uses_memory_backend_contract(self) -> None:
        from web.web_player import execute_queue_action

        r = self._queue()
        self.assertEqual(execute_queue_action("top", 2, r, area=self.area), {"ok": True})
        self.assertEqual(r.lrange(self.key, 0, -1), ["c", "a", "b"])

    def test_random_pop_uses_memory_backend_contract(self) -> None:
        from core.queue_manager import atomic_queue_pop_random

        r = _InMemoryRedis()
        for name in ("a", "b", "c"):
            r.rpush(self.key, json.dumps({"name": name}))

        raw = atomic_queue_pop_random(r, self.key)

        assert raw is not None
        popped = json.loads(str(raw))
        self.assertIn(popped["name"], ("a", "b", "c"))
        self.assertEqual(r.llen(self.key), 2)

    def test_out_of_range_index(self) -> None:
        from web.web_player import execute_queue_action

        r = self._queue()
        self.assertFalse(execute_queue_action("remove", 99, r, area=self.area)["ok"])

    def test_negative_index(self) -> None:
        """端点默认值就是 body.get("index", -1)，请求体缺 index 即命中。

        lindex 支持负索引（-1 返回队尾），光靠它判 None 会放行负数，
        接着 lset 的负数守卫抛 IndexError 穿出去。
        """
        from web.web_player import execute_queue_action

        for action in ("remove", "top"):
            for idx in (-1, -2):
                with self.subTest(action=action, idx=idx):
                    r = self._queue()
                    self.assertFalse(execute_queue_action(action, idx, r, area=self.area)["ok"])
                    self.assertEqual(
                        r.lrange(self.key, 0, -1), ["a", "b", "c"], "拒绝时不得改动队列"
                    )

    def test_unknown_action(self) -> None:
        from web.web_player import execute_queue_action

        self.assertFalse(
            execute_queue_action("explode", 0, self._queue(), area=self.area)["ok"]
        )

    def test_empty_area_never_touches_global_queue(self) -> None:
        from web.web_player import execute_queue_action

        r = self._queue()
        result = execute_queue_action("remove", 0, r, area="")

        self.assertEqual(result["code"], "playback_area_unavailable")
        self.assertEqual(r.lrange(self.key, 0, -1), ["a", "b", "c"])
        self.assertEqual(r.lrange("music:queue", 0, -1), [])

    def test_concurrent_top_and_remove_have_a_serializable_result(self) -> None:
        from web.web_player import execute_queue_action

        r = self._queue()
        r.rpush(self.key, "d")
        barrier = threading.Barrier(3)
        results = []

        def mutate(action: str, index: int) -> None:
            barrier.wait()
            results.append(execute_queue_action(action, index, r, area=self.area))

        threads = [
            threading.Thread(target=mutate, args=("remove", 1)),
            threading.Thread(target=mutate, args=("top", 2)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(results, [{"ok": True}, {"ok": True}])
        self.assertIn(
            r.lrange(self.key, 0, -1),
            (["d", "a", "c"], ["c", "b", "d"]),
        )


if __name__ == "__main__":
    unittest.main()
