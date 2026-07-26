"""Redis 降级到内存实现时的能力缺口回归测试。

降级实现原先缺 pipeline / scan / 多键 delete，几处调用点会直接抛异常或被
try/except 吞成静默失败；ping() 恒返回 True 又让 /health 在 Redis 完全挂掉时
一片绿。
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import core.queue_manager as qm
from core.queue_manager import _InMemoryRedis, is_degraded


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

        cursor, keys = self.r.scan(0, match="ai:history:*", count=100)
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
        # 内存里跑不了 LUA，调用点用 hasattr 探测后走分步回退
        self.assertFalse(hasattr(self.r, "eval"))

    def test_expire_is_deliberately_absent(self) -> None:
        # web_link_token 已有 hasattr 探测，补了反而让那处探测变成死代码
        self.assertFalse(hasattr(self.r, "expire"))


class DegradedStateTest(unittest.TestCase):
    def setUp(self) -> None:
        # 这些是模块级单例，用例间必须存档还原
        self._client = qm._redis_client
        self._retry = qm._last_redis_retry

    def tearDown(self) -> None:
        qm._redis_client = self._client
        qm._last_redis_retry = self._retry

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
                body = json.loads(web_player.health_check(request).body)
        return body["checks"]["redis"]["status"]

    def test_health_reports_memory_fallback(self) -> None:
        # ping() 恒返回 True，只看 ping 的话 Redis 完全挂掉时 /health 一片绿
        qm._redis_client = _InMemoryRedis()
        self.assertEqual(self._health_redis_status(), "degraded_memory")

    def test_health_reports_ok_on_a_real_client(self) -> None:
        qm._redis_client = object()
        self.assertEqual(self._health_redis_status(), "ok")


class QueueActionFallbackTest(unittest.TestCase):
    """没有 eval 时 execute_queue_action 应退化成分步操作，而不是崩。"""

    def _queue(self):
        r = _InMemoryRedis()
        for name in ("a", "b", "c"):
            r.rpush("music:queue", name)
        return r

    def test_remove_without_lua(self) -> None:
        from web.web_player import execute_queue_action

        r = self._queue()
        self.assertEqual(execute_queue_action("remove", 1, r), {"ok": True})
        self.assertEqual(r.lrange("music:queue", 0, -1), ["a", "c"])

    def test_top_without_lua(self) -> None:
        from web.web_player import execute_queue_action

        r = self._queue()
        self.assertEqual(execute_queue_action("top", 2, r), {"ok": True})
        self.assertEqual(r.lrange("music:queue", 0, -1), ["c", "a", "b"])

    def test_out_of_range_index_without_lua(self) -> None:
        from web.web_player import execute_queue_action

        r = self._queue()
        self.assertFalse(execute_queue_action("remove", 99, r)["ok"])

    def test_negative_index_without_lua(self) -> None:
        """端点默认值就是 body.get("index", -1)，请求体缺 index 即命中。

        lindex 支持负索引（-1 返回队尾），光靠它判 None 会放行负数，
        接着 lset 的负数守卫抛 IndexError 穿出去。
        """
        from web.web_player import execute_queue_action

        for action in ("remove", "top"):
            for idx in (-1, -2):
                with self.subTest(action=action, idx=idx):
                    r = self._queue()
                    self.assertFalse(execute_queue_action(action, idx, r)["ok"])
                    self.assertEqual(
                        r.lrange("music:queue", 0, -1), ["a", "b", "c"], "拒绝时不得改动队列"
                    )

    def test_unknown_action(self) -> None:
        from web.web_player import execute_queue_action

        self.assertFalse(execute_queue_action("explode", 0, self._queue())["ok"])


if __name__ == "__main__":
    unittest.main()
