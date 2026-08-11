
import asyncio
import json
import sys
import unittest
import uuid
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

from redis.exceptions import ResponseError

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
# 让同目录的测试助手在 discover 与直接运行两种方式下都可导入
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from async_barrier import AsyncBarrier  # noqa: E402

import core.queue_manager as qm  # noqa: E402


def _real_client(name: str) -> AsyncMock:
    """构造真实 Redis 客户端替身：命令方法均为异步。"""
    client = AsyncMock(name=name)
    client.ping = AsyncMock(return_value=True)
    client.aclose = AsyncMock()
    return client


async def _settle() -> None:
    """让出若干次事件循环，等待被唤醒的任务推进到下一个阻塞点。"""
    for _ in range(5):
        await asyncio.sleep(0)


class RedisStateMachineTestCase(unittest.IsolatedAsyncioTestCase):
    """保存并还原状态机的模块级状态。"""

    def setUp(self) -> None:
        self._saved = (
            qm._redis_client,
            qm._last_redis_retry,
            qm._redis_generation,
            qm._redis_probe_in_flight,
            qm._redis_probe_token,
            qm._redis_probe_client,
            qm._REDIS_PROBE_WAIT_TIMEOUT,
        )

    def tearDown(self) -> None:
        (
            qm._redis_client,
            qm._last_redis_retry,
            qm._redis_generation,
            qm._redis_probe_in_flight,
            qm._redis_probe_token,
            qm._redis_probe_client,
            qm._REDIS_PROBE_WAIT_TIMEOUT,
        ) = self._saved

    def _reset_state(self, client: object) -> None:
        qm._redis_client = client
        qm._last_redis_retry = 0.0
        qm._redis_probe_in_flight = False
        qm._redis_probe_token = None
        qm._redis_probe_client = None


class RedisFallbackRecoveryTest(RedisStateMachineTestCase):
    async def test_falls_back_to_memory_when_redis_down(self) -> None:
        with patch.object(qm.aioredis, "Redis", side_effect=ConnectionError("down")):
            client = await qm.get_redis_client(force_reset=True)
        self.assertIsInstance(client, qm._InMemoryRedis)

    async def test_recovers_to_real_redis_after_retry_interval(self) -> None:
        with patch.object(qm.aioredis, "Redis", side_effect=ConnectionError("down")):
            fallback = await qm.get_redis_client(force_reset=True)
        self.assertIsInstance(fallback, qm._InMemoryRedis)

        real = _real_client("RealRedis")
        with patch.object(qm.aioredis, "Redis", return_value=real):
            # 冷却期内不应重试
            self.assertIs(await qm.get_redis_client(), fallback)

            qm._last_redis_retry = 0.0
            self.assertIs(await qm.get_redis_client(), real)

    async def test_queue_manager_instances_follow_recovery(self) -> None:
        with patch.object(qm.aioredis, "Redis", side_effect=ConnectionError("down")):
            await qm.get_redis_client(force_reset=True)
            queue = qm.QueueManager(area="test-area")
            self.assertIsInstance(await queue.client(), qm._InMemoryRedis)

        real = _real_client("RealRedis")
        with patch.object(qm.aioredis, "Redis", return_value=real):
            qm._last_redis_retry = 0.0
            self.assertIs(
                await queue.client(), real, "已存在的 QueueManager 应自动切回真实 Redis"
            )

    async def test_retry_failure_keeps_memory_fallback(self) -> None:
        with patch.object(qm.aioredis, "Redis", side_effect=ConnectionError("down")):
            fallback = await qm.get_redis_client(force_reset=True)
            qm._last_redis_retry = 0.0
            self.assertIs(await qm.get_redis_client(), fallback)

    async def test_recovery_probe_waiter_receives_selected_client(self) -> None:
        fallback = qm._InMemoryRedis()
        self._reset_state(fallback)
        entered = asyncio.Event()
        release = asyncio.Event()
        real = _real_client("RecoveredRedis")

        async def blocked_probe():
            entered.set()
            await release.wait()
            return real

        with patch.object(qm, "_try_connect_redis", side_effect=blocked_probe):
            prober = asyncio.create_task(qm.get_redis_client())
            await asyncio.wait_for(entered.wait(), 1)

            waiter = asyncio.create_task(qm.get_redis_client())
            await _settle()
            self.assertFalse(
                waiter.done(), "恢复结果未定时不得交付即将退役的 fallback"
            )

            release.set()
            self.assertIs(await asyncio.wait_for(prober, 1), real)
            self.assertIs(await asyncio.wait_for(waiter, 1), real)

    async def test_unexpected_recovery_probe_error_wakes_waiters(self) -> None:
        fallback = qm._InMemoryRedis()
        self._reset_state(fallback)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def broken_probe():
            entered.set()
            await release.wait()
            raise RuntimeError("unexpected probe failure")

        with patch.object(qm, "_try_connect_redis", side_effect=broken_probe):
            prober = asyncio.create_task(qm.get_redis_client())
            await asyncio.wait_for(entered.wait(), 1)

            waiter = asyncio.create_task(qm.get_redis_client())
            await _settle()
            self.assertFalse(waiter.done())

            release.set()
            self.assertIs(await asyncio.wait_for(prober, 1), fallback)
            self.assertIs(await asyncio.wait_for(waiter, 1), fallback)

        self.assertFalse(qm._redis_probe_in_flight)

    async def test_stale_probe_cannot_overwrite_new_generation(self) -> None:
        first_fallback = qm._InMemoryRedis()
        self._reset_state(first_fallback)
        entered = asyncio.Event()
        release = asyncio.Event()
        stale = _real_client("StaleRealRedis")

        async def blocked_probe():
            entered.set()
            await release.wait()
            return stale

        with patch.object(qm, "_try_connect_redis", side_effect=blocked_probe):
            prober = asyncio.create_task(qm.get_redis_client())
            await asyncio.wait_for(entered.wait(), 1)

            new_fallback = await qm.get_redis_client(force_reset=True)
            self.assertIsInstance(new_fallback, qm._InMemoryRedis)
            self.assertIsNot(new_fallback, first_fallback)

            release.set()
            self.assertIs(await asyncio.wait_for(prober, 1), new_fallback)

        self.assertIs(qm._redis_client, new_fallback)
        stale.aclose.assert_awaited_once_with()

    async def test_probe_wait_timeout_preserves_fallback(self) -> None:
        """探测迟迟不返回时，等待必须有界，且 fallback 与其写入都要保留。"""
        fallback = qm._InMemoryRedis()
        self._reset_state(fallback)
        qm._redis_generation = 10
        entered = asyncio.Event()
        release = asyncio.Event()
        candidate = _real_client("TimedOutCandidate")

        async def blocked_probe():
            entered.set()
            await release.wait()
            return candidate

        with (
            patch.object(qm, "_REDIS_PROBE_WAIT_TIMEOUT", 0.05),
            patch.object(qm, "_try_connect_redis", side_effect=blocked_probe),
        ):
            prober = asyncio.create_task(qm.get_redis_client())
            await asyncio.wait_for(entered.wait(), 1)

            started = qm.time.monotonic()
            selected = await qm.get_redis_client()
            elapsed = qm.time.monotonic() - started

            self.assertIs(selected, fallback)
            self.assertLess(elapsed, 0.5, "探测不返回时等待也必须有界")
            self.assertIsNone(qm._redis_probe_token)
            self.assertIsNone(qm._redis_probe_client)
            self.assertFalse(qm._redis_probe_in_flight)
            self.assertEqual(qm._redis_generation, 11)

            # 超时之后写入 fallback 的数据不得被过期探测的结果冲掉
            await fallback.rpush("after-timeout", "must-survive-old-probe")

            release.set()
            await asyncio.wait_for(prober, 1)

        self.assertIs(qm._redis_client, fallback)
        self.assertEqual(await fallback.llen("after-timeout"), 1)
        candidate.aclose.assert_awaited_once_with()

    async def test_force_reset_closes_idle_real_client_without_closing_replacement(
        self,
    ) -> None:
        old = _real_client("IdleRealRedis")
        replacement = _real_client("ReplacementRedis")
        self._reset_state(old)
        qm._redis_generation = 20

        with patch.object(
            qm, "_try_connect_redis", new=AsyncMock(return_value=replacement)
        ) as probe:
            selected = await qm.get_redis_client(force_reset=True)

        self.assertIs(selected, replacement)
        self.assertIs(qm._redis_client, replacement)
        probe.assert_awaited_once_with()
        old.aclose.assert_awaited_once_with()
        replacement.aclose.assert_not_awaited()

    async def test_stale_health_probe_does_not_close_successor_probe_client(self) -> None:
        """健康探测失败后被新一代取代时，不得关闭仍在被后继探测持有的客户端。"""
        real = _real_client("SharedHealthRedis")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_failed_health_check() -> bool:
            entered.set()
            await release.wait()
            raise ConnectionError("superseded probe failure")

        real.ping = AsyncMock(side_effect=blocked_failed_health_check)
        self._reset_state(real)
        qm._redis_generation = 40

        prober = asyncio.create_task(qm.get_redis_client())
        await asyncio.wait_for(entered.wait(), 1)

        successor_token = object()
        async with qm._redis_condition:
            qm._redis_generation += 1
            qm._redis_probe_token = successor_token
            qm._redis_probe_client = real
            qm._redis_probe_in_flight = True
            qm._redis_condition.notify_all()

        release.set()
        self.assertIs(await asyncio.wait_for(prober, 1), real)

        self.assertIs(qm._redis_client, real)
        self.assertIs(qm._redis_probe_token, successor_token)
        self.assertIs(qm._redis_probe_client, real)
        real.aclose.assert_not_awaited()

        async with qm._redis_condition:
            qm._redis_probe_token = None
            qm._redis_probe_client = None
            qm._redis_probe_in_flight = False
            qm._redis_condition.notify_all()

    async def test_default_network_timeouts_are_always_applied(self) -> None:
        client = _real_client("Redis")
        with patch.object(qm.aioredis, "Redis", return_value=client) as factory:
            self.assertIs(await qm._try_connect_redis(), client)

        options = factory.call_args.kwargs
        self.assertEqual(options["socket_connect_timeout"], 3.0)
        self.assertEqual(options["socket_timeout"], 5.0)
        self.assertEqual(options["health_check_interval"], 30)

        explicit_none = _real_client("RedisWithExplicitNoneTimeouts")
        with (
            patch.object(
                qm,
                "REDIS_CONFIG",
                {"socket_connect_timeout": None, "socket_timeout": None},
            ),
            patch.object(qm.aioredis, "Redis", return_value=explicit_none) as factory2,
        ):
            self.assertIs(await qm._try_connect_redis(), explicit_none)

        # 配置显式写 None 也不得关掉恢复状态机的网络上界
        self.assertEqual(factory2.call_args.kwargs["socket_connect_timeout"], 3.0)
        self.assertEqual(factory2.call_args.kwargs["socket_timeout"], 5.0)

    async def test_failed_probe_closes_candidate_client(self) -> None:
        client = _real_client("FailedRedis")
        client.ping = AsyncMock(side_effect=TimeoutError("timeout"))

        with patch.object(qm.aioredis, "Redis", return_value=client):
            self.assertIsNone(await qm._try_connect_redis())

        client.aclose.assert_awaited_once_with()

    async def test_runtime_disconnect_enters_fallback_and_can_recover(self) -> None:
        dead = _real_client("DisconnectedRedis")
        dead.ping = AsyncMock(side_effect=ConnectionError("redis went away"))
        self._reset_state(dead)

        fallback = await qm.get_redis_client()
        self.assertIsInstance(fallback, qm._InMemoryRedis)
        self.assertTrue(qm.is_degraded())
        dead.aclose.assert_awaited_once_with()

        real = _real_client("RecoveredRedis")
        qm._last_redis_retry = 0.0
        with patch.object(qm.aioredis, "Redis", return_value=real):
            self.assertIs(await qm.get_redis_client(), real)
        self.assertFalse(qm.is_degraded())


class PlaybackControlRedisSwapTest(unittest.IsolatedAsyncioTestCase):
    """慢速平台解析结束后，提交必须使用恢复后的当前 Redis。"""

    async def test_add_song_does_not_write_to_retired_fallback(self) -> None:
        from app.services.playback.control_service import PlaybackControlService
        from core.redis_keys import QUEUE, WEB_COMMANDS, area_key

        retired = qm._InMemoryRedis()
        recovered = qm._InMemoryRedis()
        current = {"client": retired}
        resolver_entered = asyncio.Event()
        release_resolver = asyncio.Event()

        async def resolve_url(*_args, **_kwargs):
            resolver_entered.set()
            await release_resolver.wait()
            return "https://example.test/song.mp3"

        platform = Mock(name="SlowPlatform")
        platform.get_song_url = AsyncMock(side_effect=resolve_url)

        async def provider():
            return current["client"]

        service = PlaybackControlService(
            retired,
            redis_provider=provider,
            platform_resolver=lambda _name: platform,
        )
        task = asyncio.create_task(
            service.add_song(
                area="area-A", body={"id": "1", "name": "song", "artists": "artist"}
            )
        )

        await asyncio.wait_for(resolver_entered.wait(), 1)
        current["client"] = recovered
        release_resolver.set()
        result = await asyncio.wait_for(task, 2)

        self.assertEqual(result, {"ok": True, "position": 1, "name": "song"})
        queue_key = area_key(QUEUE, "area-A")
        self.assertEqual(await retired.llen(queue_key), 0)
        self.assertEqual(await retired.llen(WEB_COMMANDS), 0)
        self.assertEqual(await recovered.llen(queue_key), 1)
        self.assertEqual(await recovered.llen(WEB_COMMANDS), 1)

    async def test_concurrent_add_song_returns_distinct_atomic_positions(self) -> None:
        from app.services.playback.control_service import PlaybackControlService
        from core.redis_keys import QUEUE, WEB_COMMANDS, area_key
        from domain.playback import decode_web_command

        queue_key = area_key(QUEUE, "area-A")
        barrier = AsyncBarrier(2)
        hook_calls: list[str] = []

        class ConcurrentMemoryRedis(qm._InMemoryRedis):
            async def enqueue_song_and_notify(
                self,
                queue_key: str,
                song: object,
                commands_key: str,
                notification_template: str,
            ) -> int:
                hook_calls.append(str(song))
                # 两个请求同时抵达原子入口，位置分配必须仍然互斥
                await barrier.wait()
                return await super().enqueue_song_and_notify(
                    queue_key, song, commands_key, notification_template
                )

        client = ConcurrentMemoryRedis()
        platform = Mock(name="Platform")
        platform.get_song_url = AsyncMock(return_value="https://example.test/song.mp3")
        service = PlaybackControlService(
            client, platform_resolver=lambda _name: platform
        )

        results = await asyncio.gather(
            *(
                service.add_song(
                    area="area-A",
                    body={"id": sid, "name": f"song-{sid}", "artists": "artist"},
                )
                for sid in ("1", "2")
            )
        )

        self.assertEqual(len(hook_calls), 2, "两个请求都必须经过原子入队入口")
        self.assertEqual(sorted(cast("int", r["position"]) for r in results), [1, 2])
        self.assertEqual(await client.llen(queue_key), 2)
        self.assertEqual(await client.llen(WEB_COMMANDS), 2)

        queued = [json.loads(str(raw)) for raw in await client.lrange(queue_key, 0, -1)]
        notifications = [
            decode_web_command(str(raw))
            for raw in await client.lrange(WEB_COMMANDS, 0, -1)
        ]
        self.assertEqual([c.payload["position"] for c in notifications], [1, 2])
        self.assertEqual(
            [c.payload["name"] for c in notifications],
            [song["name"] for song in queued],
        )

    async def test_memory_wrong_type_prevalidation_prevents_partial_queue_write(
        self,
    ) -> None:
        from app.services.playback.control_service import PlaybackControlService
        from core.redis_keys import QUEUE, WEB_COMMANDS, area_key

        client = qm._InMemoryRedis()
        await client.set(WEB_COMMANDS, "not-a-list")
        platform = Mock(name="Platform")
        platform.get_song_url = AsyncMock(return_value="https://example.test/song.mp3")
        service = PlaybackControlService(
            client, platform_resolver=lambda _name: platform
        )

        with self.assertRaisesRegex(TypeError, "Web 命令键"):
            await service.add_song(
                area="area-A", body={"id": "1", "name": "song", "artists": "artist"}
            )

        self.assertEqual(await client.llen(area_key(QUEUE, "area-A")), 0)
        self.assertEqual(await client.get(WEB_COMMANDS), "not-a-list")


class QueueManagerAppendPositionTest(unittest.IsolatedAsyncioTestCase):
    async def test_add_to_queue_uses_rpush_return_value_directly(self) -> None:
        client = AsyncMock(name="Redis")
        client.rpush = AsyncMock(return_value=3)
        client.pipeline = Mock()
        with patch.object(qm, "get_redis_client", new=AsyncMock(return_value=client)):
            queue = qm.QueueManager(area="area-A")
            position = await queue.add_to_queue({"name": "song"})

        self.assertEqual(position, 2)
        client.rpush.assert_awaited_once()
        client.pipeline.assert_not_called()


class RealRedisAtomicEnqueueTest(unittest.IsolatedAsyncioTestCase):
    """CI 提供 Redis；本地无 Redis 时只跳过真实 Lua 集成层。"""

    async def asyncSetUp(self) -> None:
        self.client: Any = qm.aioredis.Redis(**dict(qm.REDIS_CONFIG))
        try:
            await self.client.ping()
        except Exception as exc:
            await self.client.aclose()
            self.skipTest(f"Redis unavailable: {exc}")
        prefix = f"oopzbot:test:{uuid.uuid4().hex}"
        self.queue_key = f"{prefix}:queue"
        self.commands_key = f"{prefix}:commands"

    async def asyncTearDown(self) -> None:
        if hasattr(self, "client"):
            await self.client.delete(self.queue_key, self.commands_key)
            await self.client.aclose()

    @staticmethod
    def _notification_template(name: str) -> str:
        from domain.playback import AreaId, AreaWebCommand, encode_web_command

        return encode_web_command(
            AreaWebCommand(
                AreaId("area-A"),
                "notify",
                {"name": name, "artists": "artist", "position": 0},
            )
        )

    async def test_lua_serializes_queue_position_and_notification_order(self) -> None:
        from core.queue_manager import atomic_enqueue_song_and_notify
        from domain.playback import decode_web_command

        results = await asyncio.gather(
            *(
                atomic_enqueue_song_and_notify(
                    self.client,
                    self.queue_key,
                    json.dumps({"name": name}),
                    self.commands_key,
                    self._notification_template(name),
                )
                for name in ("song-1", "song-2")
            )
        )

        self.assertEqual(sorted(results), [1, 2])
        queued = [
            json.loads(raw) for raw in await self.client.lrange(self.queue_key, 0, -1)
        ]
        notifications = [
            decode_web_command(raw)
            for raw in await self.client.lrange(self.commands_key, 0, -1)
        ]
        self.assertEqual([c.payload["position"] for c in notifications], [1, 2])
        self.assertEqual(
            [c.payload["name"] for c in notifications],
            [song["name"] for song in queued],
        )

    async def test_lua_prevalidates_both_key_types_before_any_write(self) -> None:
        from core.queue_manager import atomic_enqueue_song_and_notify

        await self.client.set(self.queue_key, "not-a-list")
        with self.assertRaises(ResponseError):
            await atomic_enqueue_song_and_notify(
                self.client,
                self.queue_key,
                json.dumps({"name": "song"}),
                self.commands_key,
                self._notification_template("song"),
            )
        self.assertEqual(await self.client.get(self.queue_key), "not-a-list")
        self.assertEqual(await self.client.llen(self.commands_key), 0)

        await self.client.delete(self.queue_key, self.commands_key)
        await self.client.set(self.commands_key, "not-a-list")
        with self.assertRaises(ResponseError):
            await atomic_enqueue_song_and_notify(
                self.client,
                self.queue_key,
                json.dumps({"name": "song"}),
                self.commands_key,
                self._notification_template("song"),
            )
        self.assertEqual(await self.client.llen(self.queue_key), 0)
        self.assertEqual(await self.client.get(self.commands_key), "not-a-list")


class ConversationMemoryProviderTest(unittest.IsolatedAsyncioTestCase):
    """ConversationMemory 应通过 provider 现取客户端，跟随 Redis 恢复。"""

    async def test_memory_follows_provider_swap(self) -> None:
        from services.conversation_memory import ConversationMemory

        first = AsyncMock(name="MemoryClient")
        first.get = AsyncMock(return_value=None)
        second = AsyncMock(name="RealClient")
        second.get = AsyncMock(return_value=None)
        current = {"client": first}

        async def provider():
            return current["client"]

        memory = ConversationMemory(provider, max_rounds=5)

        await memory.get_history("user-1", "channel-1")
        first.get.assert_awaited_once()

        current["client"] = second
        await memory.get_history("user-1", "channel-1")
        second.get.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
