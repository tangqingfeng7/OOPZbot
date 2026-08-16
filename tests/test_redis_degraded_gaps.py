
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
# 让同目录的测试助手在 discover 与直接运行两种方式下都可导入
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from async_barrier import AsyncBarrier  # noqa: E402

import core.queue_manager as qm  # noqa: E402
from core.queue_manager import _InMemoryRedis, is_degraded  # noqa: E402


class InMemoryRedisCapabilityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.r = _InMemoryRedis()

    async def test_delete_accepts_multiple_keys_and_returns_count(self) -> None:
        await self.r.set("a", "1")
        await self.r.set("b", "2")
        await self.r.rpush("c", "x")

        self.assertEqual(await self.r.delete("a", "b", "c", "missing"), 3)
        self.assertIsNone(await self.r.get("a"))

    async def test_scan_returns_matching_keys_and_terminates(self) -> None:
        await self.r.set("cache:item:u1", "x")
        await self.r.set("cache:item:u2", "y")
        await self.r.set("music:queue", "z")

        cursor, keys = await self.r.scan(0, match="cache:item:*", count=100)

        self.assertEqual(cursor, 0, "游标必须归零，否则调用方的 while 循环不退出")
        self.assertEqual(sorted(keys), ["cache:item:u1", "cache:item:u2"])

    async def test_scan_then_delete_matches_the_real_call_site(self) -> None:
        for i in range(3):
            await self.r.set(f"cache:item:{i}", "x")

        _cursor, keys = await self.r.scan(0, match="cache:item:*", count=100)
        removed = await self.r.delete(*keys)

        self.assertEqual(removed, 3)

    async def test_pipeline_executes_in_order(self) -> None:
        pipe = self.r.pipeline(transaction=False)
        pipe.set("k", "v")
        pipe.get("k")
        pipe.llen("nope")

        self.assertEqual(await pipe.execute(), [None, "v", 0])

    async def test_pipeline_is_chainable_and_resets_after_execute(self) -> None:
        pipe = self.r.pipeline(transaction=False)
        pipe.set("a", "1").set("b", "2")
        await pipe.execute()

        self.assertEqual(await pipe.execute(), [], "execute 之后应清空已排队的命令")

    def test_eval_is_deliberately_absent(self) -> None:
        # 内存里不解释 Lua，原子队列操作走明确的内存后端契约。
        self.assertFalse(hasattr(self.r, "eval"))

    def test_expire_is_deliberately_absent(self) -> None:
        # web_link_token 已有 hasattr 探测，补了反而让那处探测变成死代码
        self.assertFalse(hasattr(self.r, "expire"))


class DegradedStateTest(unittest.IsolatedAsyncioTestCase):
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

    async def _health_redis_status(self) -> str:
        import web.web_player as web_player

        with mock.patch.object(
            web_player, "get_redis", new=mock.AsyncMock(return_value=_InMemoryRedis())
        ):
            request = mock.Mock()
            with mock.patch.object(
                web_player, "_is_admin_authorized", new=mock.AsyncMock(return_value=True)
            ):
                response = await web_player.health_check(request)
        return json.loads(bytes(response.body))["checks"]["redis"]["status"]

    async def test_health_reports_memory_fallback(self) -> None:
        # ping() 恒返回 True，只看 ping 的话 Redis 完全挂掉时 /health 一片绿
        qm._redis_client = _InMemoryRedis()
        self.assertEqual(await self._health_redis_status(), "degraded_memory")

    async def test_health_reports_ok_on_a_real_client(self) -> None:
        qm._redis_client = object()
        self.assertEqual(await self._health_redis_status(), "ok")


class QueueActionAtomicityTest(unittest.IsolatedAsyncioTestCase):
    """内存降级后仍必须通过后端契约原子修改域队列。"""

    area = "area-A"
    key = "music:area-A:queue"

    async def _queue(self):
        r = _InMemoryRedis()
        for name in ("a", "b", "c"):
            await r.rpush(self.key, name)
        return r

    async def test_remove_uses_memory_backend_contract(self) -> None:
        from web.web_player import execute_queue_action

        r = await self._queue()
        self.assertEqual(await execute_queue_action("remove", 1, r, area=self.area), {"ok": True})
        self.assertEqual(await r.lrange(self.key, 0, -1), ["a", "c"])

    async def test_top_uses_memory_backend_contract(self) -> None:
        from web.web_player import execute_queue_action

        r = await self._queue()
        self.assertEqual(await execute_queue_action("top", 2, r, area=self.area), {"ok": True})
        self.assertEqual(await r.lrange(self.key, 0, -1), ["c", "a", "b"])

    async def test_random_pop_uses_memory_backend_contract(self) -> None:
        from core.queue_manager import atomic_queue_pop_random

        r = _InMemoryRedis()
        for name in ("a", "b", "c"):
            await r.rpush(self.key, json.dumps({"name": name}))

        raw = await atomic_queue_pop_random(r, self.key)

        assert raw is not None
        popped = json.loads(str(raw))
        self.assertIn(popped["name"], ("a", "b", "c"))
        self.assertEqual(await r.llen(self.key), 2)

    async def test_out_of_range_index(self) -> None:
        from web.web_player import execute_queue_action

        r = await self._queue()
        self.assertFalse((await execute_queue_action("remove", 99, r, area=self.area))["ok"])

    async def test_negative_index(self) -> None:
        """端点默认值就是 body.get("index", -1)，请求体缺 index 即命中。

        lindex 支持负索引（-1 返回队尾），光靠它判 None 会放行负数，
        接着 lset 的负数守卫抛 IndexError 穿出去。
        """
        from web.web_player import execute_queue_action

        for action in ("remove", "top"):
            for idx in (-1, -2):
                with self.subTest(action=action, idx=idx):
                    r = await self._queue()
                    self.assertFalse((await execute_queue_action(action, idx, r, area=self.area))["ok"])
                    self.assertEqual(
                        await r.lrange(self.key, 0, -1), ["a", "b", "c"], "拒绝时不得改动队列"
                    )

    async def test_unknown_action(self) -> None:
        from web.web_player import execute_queue_action

        self.assertFalse(
            (await execute_queue_action("explode", 0, await self._queue(), area=self.area))["ok"]
        )

    async def test_empty_area_never_touches_global_queue(self) -> None:
        from web.web_player import execute_queue_action

        r = await self._queue()
        result = await execute_queue_action("remove", 0, r, area="")

        self.assertEqual(result["code"], "playback_area_unavailable")
        self.assertEqual(await r.lrange(self.key, 0, -1), ["a", "b", "c"])
        self.assertEqual(await r.lrange("music:queue", 0, -1), [])

    async def test_concurrent_top_and_remove_have_a_serializable_result(self) -> None:
        from web.web_player import execute_queue_action

        r = await self._queue()
        await r.rpush(self.key, "d")
        barrier = AsyncBarrier(2)

        async def mutate(action: str, index: int):
            # 两个变更同时抵达，结果必须可串行化
            await barrier.wait()
            return await execute_queue_action(action, index, r, area=self.area)

        results = list(await asyncio.gather(mutate("remove", 1), mutate("top", 2)))

        self.assertEqual(results, [{"ok": True}, {"ok": True}])
        self.assertIn(
            await r.lrange(self.key, 0, -1),
            (["d", "a", "c"], ["c", "b", "d"]),
        )


if __name__ == "__main__":
    unittest.main()
