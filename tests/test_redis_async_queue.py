
import asyncio
import sys
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import core.queue_manager as qm  # noqa: E402


class AsyncQueueTestCase(unittest.IsolatedAsyncioTestCase):
    """每个用例都从一个全新的内存降级后端开始。"""

    async def asyncSetUp(self) -> None:
        patcher = mock.patch.object(
            qm, "_try_connect_redis", new=mock.AsyncMock(return_value=None)
        )
        self.connect = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = await qm.get_redis_client(force_reset=True)
        self.queue = qm.QueueManager("test-area")


class QueueManagerAsyncTest(AsyncQueueTestCase):
    async def test_enqueue_and_pop_round_trip(self) -> None:
        self.assertEqual(await self.queue.add_to_queue({"name": "A"}), 0)
        self.assertEqual(await self.queue.add_to_queue({"name": "B"}), 1)
        self.assertEqual(await self.queue.get_queue_length(), 2)
        self.assertEqual([s["name"] for s in await self.queue.get_queue()], ["A", "B"])

        peeked = await self.queue.peek_next()
        assert peeked is not None
        self.assertEqual(peeked["name"], "A")
        # peek 不消费
        self.assertEqual(await self.queue.get_queue_length(), 2)

        popped = await self.queue.play_next()
        assert popped is not None
        self.assertEqual(popped["name"], "A")
        self.assertEqual(await self.queue.get_queue_length(), 1)

    async def test_clear_and_remove(self) -> None:
        for name in ("A", "B", "C"):
            await self.queue.add_to_queue({"name": name})

        self.assertTrue(await self.queue.remove_from_queue(1))
        self.assertEqual([s["name"] for s in await self.queue.get_queue()], ["A", "C"])
        self.assertFalse(await self.queue.remove_from_queue(99))

        await self.queue.clear_queue()
        self.assertEqual(await self.queue.get_queue_length(), 0)

    async def test_current_play_state_and_mode(self) -> None:
        await self.queue.set_current({"name": "A"})
        current = await self.queue.get_current()
        assert current is not None
        self.assertEqual(current["name"], "A")
        await self.queue.clear_current()
        self.assertIsNone(await self.queue.get_current())

        await self.queue.set_play_state({"playing": True})
        self.assertEqual(await self.queue.get_play_state(), {"playing": True})
        await self.queue.clear_play_state()
        self.assertIsNone(await self.queue.get_play_state())

        self.assertIsNone(await self.queue.get_play_mode())
        await self.queue.set_play_mode("shuffle")
        self.assertEqual(await self.queue.get_play_mode(), "shuffle")

    async def test_areas_are_isolated(self) -> None:
        other = qm.QueueManager("other-area")
        await self.queue.add_to_queue({"name": "A"})
        self.assertEqual(await other.get_queue_length(), 0)


class InMemoryBackendTest(AsyncQueueTestCase):
    async def test_atomic_enqueue_assigns_position_and_notification(self) -> None:
        position = await qm.atomic_enqueue_song_and_notify(
            self.client, "qk", "song-1", "ck", '{"payload":{}}'
        )
        self.assertEqual(position, 1)
        self.assertEqual(await self.client.llen("qk"), 1)
        self.assertEqual(await self.client.llen("ck"), 1)

    async def test_atomic_enqueue_rejects_shared_key(self) -> None:
        with self.assertRaises(ValueError):
            await qm.atomic_enqueue_song_and_notify(
                self.client, "same", "song", "same", '{"payload":{}}'
            )

    async def test_move_to_front_and_pop_random(self) -> None:
        await self.client.rpush("k", "a", "b", "c")
        self.assertTrue(await qm.atomic_queue_move_to_front(self.client, "k", 2))
        self.assertEqual(await self.client.lrange("k", 0, -1), ["c", "a", "b"])

        popped = await qm.atomic_queue_pop_random(self.client, "k")
        self.assertIn(popped, {"a", "b", "c"})
        self.assertEqual(await self.client.llen("k"), 2)

    async def test_pipeline_replays_in_order(self) -> None:
        pipe = self.client.pipeline()
        pipe.set("k1", "v1").get("k1").rpush("l1", "x").llen("l1")
        self.assertEqual(await pipe.execute(), [None, "v1", 1, 1])

    async def test_blpop_returns_none_after_timeout(self) -> None:
        self.assertIsNone(await self.client.blpop("empty", timeout=1))

    async def test_blpop_wakes_on_push(self) -> None:
        async def push_later() -> None:
            await asyncio.sleep(0.05)
            await self.client.rpush("bq", "value")

        task = asyncio.create_task(push_later())
        self.assertEqual(await self.client.blpop("bq", timeout=5), ("bq", "value"))
        await task

    async def test_scan_and_delete(self) -> None:
        await self.client.set("p:1", "a")
        await self.client.set("p:2", "b")
        cursor, keys = await self.client.scan(match="p:*")
        self.assertEqual(cursor, 0)
        self.assertEqual(sorted(keys), ["p:1", "p:2"])
        self.assertEqual(await self.client.delete("p:1", "p:2"), 2)

    async def test_set_max_float_keeps_maximum(self) -> None:
        # set_max_float 只是内存后端提供的能力，协议未声明；真实 Redis 侧由
        # web_link_token 用 try/except 探测后回退到 get/set。
        client = cast(qm._InMemoryRedis, self.client)
        self.assertEqual(await client.set_max_float("f", 2.5), 2.5)
        self.assertEqual(await client.set_max_float("f", 1.0), 2.5)
        self.assertEqual(await client.set_max_float("f", 4.0), 4.0)


class ConnectionRecoveryTest(AsyncQueueTestCase):
    async def test_falls_back_to_memory_when_connect_fails(self) -> None:
        self.assertTrue(qm.is_degraded())
        # 降级期写入仍然成功，只是不落到真实 Redis
        self.assertEqual(await self.queue.add_to_queue({"name": "A"}), 0)

    async def test_retry_interval_suppresses_repeated_probes(self) -> None:
        self.connect.reset_mock()
        await qm.get_redis_client()
        await qm.get_redis_client()
        self.connect.assert_not_awaited()

    async def test_recovers_to_real_redis_after_retry_interval(self) -> None:
        recovered = mock.AsyncMock()
        recovered.ping = mock.AsyncMock(return_value=True)
        self.connect.return_value = recovered

        # 到达重试间隔后应重新探测并切回真实客户端
        qm._last_redis_retry = 0.0
        client = await qm.get_redis_client()

        self.assertIs(client, recovered)
        self.assertFalse(qm.is_degraded())

    async def test_existing_queue_manager_follows_recovery(self) -> None:
        recovered = mock.AsyncMock()
        recovered.ping = mock.AsyncMock(return_value=True)
        self.connect.return_value = recovered

        qm._last_redis_retry = 0.0
        # 降级期创建的实例，恢复后必须跟随切到新客户端
        self.assertIs(await self.queue.client(), recovered)

    async def test_health_probe_failure_switches_back_to_memory(self) -> None:
        broken = mock.AsyncMock()
        broken.ping = mock.AsyncMock(side_effect=ConnectionError("链路中断"))
        self.connect.return_value = broken

        qm._last_redis_retry = 0.0
        self.assertIs(await qm.get_redis_client(), broken)

        # 下一次交付前的健康探测失败，应原子切回内存实现
        client = await qm.get_redis_client()
        self.assertTrue(qm.is_degraded())
        self.assertIsInstance(client, qm._InMemoryRedis)

    async def test_force_reset_starts_a_fresh_memory_backend(self) -> None:
        await self.queue.add_to_queue({"name": "A"})
        await qm.get_redis_client(force_reset=True)
        self.assertTrue(qm.is_degraded())
        self.assertEqual(await self.queue.get_queue_length(), 0)


if __name__ == "__main__":
    unittest.main()
