import json
import sys
import threading
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from redis.exceptions import ResponseError

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import core.queue_manager as qm  # noqa: E402


class RedisFallbackRecoveryTest(unittest.TestCase):
    """内存降级后应周期性探测真实 Redis 并自动切回。"""

    def setUp(self) -> None:
        self._saved_client = qm._redis_client
        self._saved_retry = qm._last_redis_retry
        self._saved_generation = qm._redis_generation
        self._saved_probe = qm._redis_probe_in_flight
        self._saved_probe_token = qm._redis_probe_token
        self._saved_probe_client = qm._redis_probe_client

    def tearDown(self) -> None:
        qm._redis_client = self._saved_client
        qm._last_redis_retry = self._saved_retry
        qm._redis_generation = self._saved_generation
        qm._redis_probe_in_flight = self._saved_probe
        qm._redis_probe_token = self._saved_probe_token
        qm._redis_probe_client = self._saved_probe_client

    def test_falls_back_to_memory_when_redis_down(self) -> None:
        with patch.object(qm.redis, "Redis", side_effect=ConnectionError("down")):
            client = qm.get_redis_client(force_reset=True)

        self.assertIsInstance(client, qm._InMemoryRedis)

    def test_recovers_to_real_redis_after_retry_interval(self) -> None:
        with patch.object(qm.redis, "Redis", side_effect=ConnectionError("down")):
            fallback = qm.get_redis_client(force_reset=True)
        self.assertIsInstance(fallback, qm._InMemoryRedis)

        real_client = Mock(name="RealRedis")
        real_client.ping = Mock(return_value=True)
        # 冷却期内不应重试
        with patch.object(qm.redis, "Redis", return_value=real_client):
            still_memory = qm.get_redis_client()
            self.assertIs(still_memory, fallback)

            # 冷却期过后自动切回真实 Redis
            qm._last_redis_retry = 0.0
            recovered = qm.get_redis_client()

        self.assertIs(recovered, real_client)

    def test_queue_manager_instances_follow_recovery(self) -> None:
        with patch.object(qm.redis, "Redis", side_effect=ConnectionError("down")):
            qm.get_redis_client(force_reset=True)
            queue = qm.QueueManager(area="test-area")
            self.assertIsInstance(queue.redis, qm._InMemoryRedis)

        real_client = Mock(name="RealRedis")
        real_client.ping = Mock(return_value=True)
        with patch.object(qm.redis, "Redis", return_value=real_client):
            qm._last_redis_retry = 0.0
            self.assertIs(queue.redis, real_client, "已存在的 QueueManager 应自动切回真实 Redis")

    def test_retry_failure_keeps_memory_fallback(self) -> None:
        with patch.object(qm.redis, "Redis", side_effect=ConnectionError("down")):
            fallback = qm.get_redis_client(force_reset=True)
            qm._last_redis_retry = 0.0
            still_memory = qm.get_redis_client()

        self.assertIs(still_memory, fallback)

    def test_recovery_probe_waiter_receives_selected_client(self) -> None:
        fallback = qm._InMemoryRedis()
        qm._redis_client = fallback
        qm._last_redis_retry = 0.0
        qm._redis_probe_in_flight = False
        qm._redis_probe_token = None
        qm._redis_probe_client = None
        entered = threading.Event()
        release = threading.Event()
        waiter_started = threading.Event()
        waiter_returned = threading.Event()
        real_client = Mock(name="RecoveredRedis")
        real_client.ping.return_value = True

        def blocked_probe():
            entered.set()
            release.wait(timeout=2)
            return real_client

        results: list[object] = []

        def wait_for_client() -> None:
            waiter_started.set()
            results.append(qm.get_redis_client())
            waiter_returned.set()

        probe_worker = threading.Thread(target=qm.get_redis_client)
        waiter = threading.Thread(target=wait_for_client)
        with patch.object(qm, "_try_connect_redis", side_effect=blocked_probe):
            probe_worker.start()
            self.assertTrue(entered.wait(timeout=1))
            waiter.start()
            self.assertTrue(waiter_started.wait(timeout=1))
            self.assertFalse(
                waiter_returned.wait(timeout=0.05),
                "恢复结果未定时不得交付即将退役的 fallback",
            )
            release.set()
            probe_worker.join(timeout=2)
            waiter.join(timeout=2)

        self.assertFalse(probe_worker.is_alive())
        self.assertFalse(waiter.is_alive())
        self.assertEqual(results, [real_client])

    def test_unexpected_recovery_probe_error_wakes_waiters(self) -> None:
        fallback = qm._InMemoryRedis()
        qm._redis_client = fallback
        qm._last_redis_retry = 0.0
        qm._redis_probe_in_flight = False
        qm._redis_probe_token = None
        qm._redis_probe_client = None
        entered = threading.Event()
        release = threading.Event()
        waiter_started = threading.Event()
        waiter_returned = threading.Event()
        results: list[object] = []

        def broken_probe():
            entered.set()
            release.wait(timeout=2)
            raise RuntimeError("unexpected probe failure")

        def wait_for_client() -> None:
            waiter_started.set()
            results.append(qm.get_redis_client())
            waiter_returned.set()

        probe_worker = threading.Thread(target=lambda: results.append(qm.get_redis_client()))
        waiter = threading.Thread(target=wait_for_client)
        with patch.object(qm, "_try_connect_redis", side_effect=broken_probe):
            probe_worker.start()
            self.assertTrue(entered.wait(timeout=1))
            waiter.start()
            self.assertTrue(waiter_started.wait(timeout=1))
            self.assertFalse(waiter_returned.wait(timeout=0.05))
            release.set()
            probe_worker.join(timeout=2)
            waiter.join(timeout=2)

        self.assertFalse(probe_worker.is_alive())
        self.assertFalse(waiter.is_alive())
        self.assertEqual(results, [fallback, fallback])
        self.assertFalse(qm._redis_probe_in_flight)

    def test_stale_probe_cannot_overwrite_new_generation(self) -> None:
        first_fallback = qm._InMemoryRedis()
        qm._redis_client = first_fallback
        qm._last_redis_retry = 0.0
        qm._redis_probe_in_flight = False
        qm._redis_probe_token = None
        qm._redis_probe_client = None
        entered = threading.Event()
        release = threading.Event()
        real_client = Mock(name="StaleRealRedis")
        result = []

        def blocked_probe():
            entered.set()
            release.wait(timeout=2)
            return real_client

        worker = threading.Thread(target=lambda: result.append(qm.get_redis_client()))
        with patch.object(qm, "_try_connect_redis", side_effect=blocked_probe):
            worker.start()
            self.assertTrue(entered.wait(timeout=1))
            new_fallback = qm.get_redis_client(force_reset=True)
            self.assertIsInstance(new_fallback, qm._InMemoryRedis)
            self.assertIsNot(new_fallback, first_fallback)
            release.set()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertIs(qm._redis_client, new_fallback)
        self.assertEqual(result, [new_fallback])
        real_client.close.assert_called_once_with()

    def test_probe_wait_timeout_preserves_fallback_and_old_probe_cannot_clear_new(self) -> None:
        fallback = qm._InMemoryRedis()
        qm._redis_client = fallback
        qm._last_redis_retry = 0.0
        qm._redis_generation = 10
        qm._redis_probe_in_flight = False
        qm._redis_probe_token = None
        qm._redis_probe_client = None
        old_entered = threading.Event()
        old_release = threading.Event()
        new_entered = threading.Event()
        new_release = threading.Event()
        call_lock = threading.Lock()
        call_count = 0
        old_closed = threading.Event()
        old_client = Mock(name="TimedOutCandidate")
        old_client.close.side_effect = old_closed.set
        new_client = Mock(name="CurrentCandidate")

        def blocked_probe():
            nonlocal call_count
            with call_lock:
                probe_index = call_count
                call_count += 1
            if probe_index == 0:
                old_entered.set()
                old_release.wait()
                return old_client
            if probe_index == 1:
                new_entered.set()
                new_release.wait()
                return new_client
            raise AssertionError("不应启动第三次探测")

        old_results: list[object] = []
        new_results: list[object] = []
        old_worker = threading.Thread(
            target=lambda: old_results.append(qm.get_redis_client())
        )
        new_worker = threading.Thread(
            target=lambda: new_results.append(qm.get_redis_client())
        )

        with (
            patch.object(qm, "_REDIS_PROBE_WAIT_TIMEOUT", 0.05),
            patch.object(qm, "_try_connect_redis", side_effect=blocked_probe),
        ):
            try:
                old_worker.start()
                self.assertTrue(old_entered.wait(timeout=1))

                started_at = qm.time.monotonic()
                selected = qm.get_redis_client()
                elapsed = qm.time.monotonic() - started_at

                self.assertIs(selected, fallback)
                self.assertLess(elapsed, 0.5, "探测不返回时等待也必须有界")
                self.assertIsNone(qm._redis_probe_token)
                self.assertIsNone(qm._redis_probe_client)
                self.assertFalse(qm._redis_probe_in_flight)
                self.assertEqual(qm._redis_generation, 11)
                fallback.rpush("after-timeout", "must-survive-old-probe")
                qm._REDIS_PROBE_WAIT_TIMEOUT = 1.0

                # 模拟冷却期过后的新探测。旧探测此时仍卡住，
                # 因此两个 probe 会短暂并存，令牌必须隔离它们的收尾。
                qm._last_redis_retry = 0.0
                new_worker.start()
                self.assertTrue(new_entered.wait(timeout=1))
                current_token = qm._redis_probe_token
                self.assertIsNotNone(current_token)
                self.assertIs(qm._redis_probe_client, fallback)
                self.assertTrue(qm._redis_probe_in_flight)

                old_release.set()
                self.assertTrue(old_closed.wait(timeout=1))
                self.assertTrue(old_worker.is_alive(), "陈旧 probe 必须继续等待当前 probe")
                self.assertIs(qm._redis_client, fallback)
                self.assertEqual(fallback.llen("after-timeout"), 1)
                self.assertIs(qm._redis_probe_token, current_token)
                self.assertIs(qm._redis_probe_client, fallback)
                self.assertTrue(qm._redis_probe_in_flight)
                old_client.close.assert_called_once_with()

                new_release.set()
                new_worker.join(timeout=1)
                old_worker.join(timeout=1)
                self.assertFalse(new_worker.is_alive())
                self.assertFalse(old_worker.is_alive())
            finally:
                old_release.set()
                new_release.set()
                old_worker.join(timeout=1)
                if new_worker.ident is not None:
                    new_worker.join(timeout=1)

        self.assertEqual(old_results, [new_client])
        self.assertEqual(new_results, [new_client])
        self.assertIs(qm._redis_client, new_client)
        self.assertIsNone(qm._redis_probe_token)
        self.assertIsNone(qm._redis_probe_client)
        self.assertFalse(qm._redis_probe_in_flight)

    def test_force_reset_closes_idle_real_client_without_closing_replacement(self) -> None:
        old_client = Mock(name="IdleRealRedis")
        replacement = Mock(name="ReplacementRedis")
        qm._redis_client = old_client
        qm._redis_generation = 20
        qm._last_redis_retry = 0.0
        qm._redis_probe_in_flight = False
        qm._redis_probe_token = None
        qm._redis_probe_client = None

        with patch.object(qm, "_try_connect_redis", return_value=replacement) as probe:
            selected = qm.get_redis_client(force_reset=True)

        self.assertIs(selected, replacement)
        self.assertIs(qm._redis_client, replacement)
        probe.assert_called_once_with()
        old_client.close.assert_called_once_with()
        replacement.close.assert_not_called()

    def _assert_stale_health_probe_closes_old_client(
        self,
        *,
        healthy: bool,
    ) -> None:
        old_entered = threading.Event()
        old_release = threading.Event()
        old_closed = threading.Event()
        new_entered = threading.Event()
        new_release = threading.Event()
        old_client = Mock(name=f"StaleHealthRedis-{healthy}")
        new_client = Mock(name=f"RecoveredRedis-{healthy}")

        def blocked_health_check() -> bool:
            old_entered.set()
            if not old_release.wait(timeout=2):
                raise RuntimeError("等待释放旧健康探测超时")
            if healthy:
                return True
            raise ConnectionError("stale health failure")

        def blocked_recovery_probe() -> object:
            new_entered.set()
            if not new_release.wait(timeout=2):
                raise RuntimeError("等待释放新恢复探测超时")
            return new_client

        old_client.ping.side_effect = blocked_health_check
        old_client.close.side_effect = old_closed.set
        qm._redis_client = old_client
        qm._redis_generation = 30
        qm._last_redis_retry = 0.0
        qm._redis_probe_in_flight = False
        qm._redis_probe_token = None
        qm._redis_probe_client = None
        health_results: list[object] = []
        recovery_results: list[object] = []
        worker_errors: list[BaseException] = []

        def run_health_probe() -> None:
            try:
                health_results.append(qm.get_redis_client())
            except BaseException as exc:
                worker_errors.append(exc)

        def run_recovery_probe() -> None:
            try:
                recovery_results.append(qm.get_redis_client())
            except BaseException as exc:
                worker_errors.append(exc)

        health_worker = threading.Thread(target=run_health_probe)
        recovery_worker = threading.Thread(target=run_recovery_probe)
        successor_token: object | None = None
        with (
            patch.object(qm, "_REDIS_PROBE_WAIT_TIMEOUT", 0.05),
            patch.object(
                qm,
                "_try_connect_redis",
                side_effect=blocked_recovery_probe,
            ),
        ):
            try:
                health_worker.start()
                self.assertTrue(old_entered.wait(timeout=1))
                fallback = qm.get_redis_client(force_reset=True)
                self.assertIsInstance(fallback, qm._InMemoryRedis)
                old_client.close.assert_not_called()
                self.assertIs(qm._redis_probe_client, old_client)

                # 等待者超时后会使旧令牌失效，但保留 fallback。
                self.assertIs(qm.get_redis_client(), fallback)
                self.assertIsNone(qm._redis_probe_token)
                self.assertIsNone(qm._redis_probe_client)

                # 在旧健康探测返回前启动一个真实的新恢复探测。
                qm._last_redis_retry = 0.0
                qm._REDIS_PROBE_WAIT_TIMEOUT = 1.0
                recovery_worker.start()
                self.assertTrue(new_entered.wait(timeout=1))
                successor_token = qm._redis_probe_token
                self.assertIsNotNone(successor_token)
                self.assertIs(qm._redis_probe_client, fallback)

                old_release.set()
                self.assertTrue(old_closed.wait(timeout=1))
                old_client.close.assert_called_once_with()
                self.assertIs(qm._redis_client, fallback)
                self.assertIs(qm._redis_probe_token, successor_token)
                self.assertIs(qm._redis_probe_client, fallback)
                new_client.close.assert_not_called()

                new_release.set()
                health_worker.join(timeout=1)
                recovery_worker.join(timeout=1)
            finally:
                old_release.set()
                new_release.set()
                if health_worker.ident is not None:
                    health_worker.join(timeout=1)
                if recovery_worker.ident is not None:
                    recovery_worker.join(timeout=1)

        self.assertFalse(health_worker.is_alive())
        self.assertFalse(recovery_worker.is_alive())
        self.assertEqual(worker_errors, [])
        self.assertEqual(health_results, [new_client])
        self.assertEqual(recovery_results, [new_client])
        self.assertIs(qm._redis_client, new_client)
        self.assertIsNone(qm._redis_probe_token)
        self.assertIsNone(qm._redis_probe_client)
        new_client.close.assert_not_called()

    def test_stale_health_probe_closes_old_client_without_clearing_new_probe(
        self,
    ) -> None:
        for healthy in (True, False):
            with self.subTest(healthy=healthy):
                self._assert_stale_health_probe_closes_old_client(healthy=healthy)

    def test_stale_health_probe_does_not_close_current_successor_probe_client(
        self,
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        real_client = Mock(name="SharedHealthRedis")

        def blocked_failed_health_check() -> bool:
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError("等待释放健康探测超时")
            raise ConnectionError("superseded probe failure")

        real_client.ping.side_effect = blocked_failed_health_check
        qm._redis_client = real_client
        qm._redis_generation = 40
        qm._last_redis_retry = 0.0
        qm._redis_probe_in_flight = False
        qm._redis_probe_token = None
        qm._redis_probe_client = None
        results: list[object] = []
        errors: list[BaseException] = []

        def run_probe() -> None:
            try:
                results.append(qm.get_redis_client())
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run_probe)
        successor_token: object | None = None
        try:
            worker.start()
            self.assertTrue(entered.wait(timeout=1))
            successor_token = object()
            with qm._redis_condition:
                qm._redis_generation += 1
                qm._redis_probe_token = successor_token
                qm._redis_probe_client = real_client
                qm._redis_probe_in_flight = True
                qm._redis_condition.notify_all()

            release.set()
            worker.join(timeout=1)

            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results, [real_client])
            self.assertIs(qm._redis_client, real_client)
            self.assertIs(qm._redis_probe_token, successor_token)
            self.assertIs(qm._redis_probe_client, real_client)
            real_client.close.assert_not_called()
        finally:
            release.set()
            if worker.ident is not None:
                worker.join(timeout=1)
            with qm._redis_condition:
                if successor_token is not None and qm._redis_probe_token is successor_token:
                    qm._redis_probe_token = None
                    qm._redis_probe_client = None
                    qm._redis_probe_in_flight = False
                    qm._redis_condition.notify_all()

    def test_default_network_timeouts_are_always_applied(self) -> None:
        client = Mock(name="Redis")
        client.ping.return_value = True
        with patch.object(qm.redis, "Redis", return_value=client) as redis_factory:
            self.assertIs(qm._try_connect_redis(), client)

        options = redis_factory.call_args.kwargs
        self.assertEqual(options["socket_connect_timeout"], 3.0)
        self.assertEqual(options["socket_timeout"], 5.0)
        self.assertEqual(options["health_check_interval"], 30)

        explicit_none_client = Mock(name="RedisWithExplicitNoneTimeouts")
        explicit_none_client.ping.return_value = True
        with (
            patch.object(
                qm,
                "REDIS_CONFIG",
                {"socket_connect_timeout": None, "socket_timeout": None},
            ),
            patch.object(qm.redis, "Redis", return_value=explicit_none_client) as factory,
        ):
            self.assertIs(qm._try_connect_redis(), explicit_none_client)

        explicit_none_options = factory.call_args.kwargs
        self.assertEqual(explicit_none_options["socket_connect_timeout"], 3.0)
        self.assertEqual(explicit_none_options["socket_timeout"], 5.0)

    def test_failed_probe_closes_candidate_client(self) -> None:
        client = Mock(name="FailedRedis")
        client.ping.side_effect = TimeoutError("timeout")

        with patch.object(qm.redis, "Redis", return_value=client):
            self.assertIsNone(qm._try_connect_redis())

        client.close.assert_called_once_with()

    def test_runtime_disconnect_enters_fallback_and_can_recover(self) -> None:
        dead_client = Mock(name="DisconnectedRedis")
        dead_client.ping.side_effect = ConnectionError("redis went away")
        qm._redis_client = dead_client

        fallback = qm.get_redis_client()

        self.assertIsInstance(fallback, qm._InMemoryRedis)
        self.assertTrue(qm.is_degraded())
        dead_client.close.assert_called_once_with()

        real_client = Mock(name="RecoveredRedis")
        real_client.ping.return_value = True
        qm._last_redis_retry = 0.0
        with patch.object(qm.redis, "Redis", return_value=real_client):
            recovered = qm.get_redis_client()

        self.assertIs(recovered, real_client)
        self.assertFalse(qm.is_degraded())


class PlaybackControlRedisSwapTest(unittest.TestCase):
    """慢速平台解析结束后，提交必须使用恢复后的当前 Redis。"""

    def test_add_song_does_not_write_to_retired_fallback(self) -> None:
        from app.services.playback.control_service import PlaybackControlService
        from core.redis_keys import QUEUE, WEB_COMMANDS, area_key

        retired = qm._InMemoryRedis()
        recovered = qm._InMemoryRedis()
        current = {"client": retired}
        resolver_entered = threading.Event()
        release_resolver = threading.Event()
        platform = Mock(name="SlowPlatform")

        def resolve_url(*_args, **_kwargs):
            resolver_entered.set()
            self.assertTrue(release_resolver.wait(timeout=2))
            return "https://example.test/song.mp3"

        platform.get_song_url.side_effect = resolve_url
        service = PlaybackControlService(
            retired,
            redis_provider=lambda: current["client"],
            platform_resolver=lambda _name: platform,
        )
        results: list[dict[str, object]] = []
        worker = threading.Thread(
            target=lambda: results.append(
                service.add_song(
                    area="area-A",
                    body={"id": "1", "name": "song", "artists": "artist"},
                )
            )
        )

        worker.start()
        self.assertTrue(resolver_entered.wait(timeout=1))
        current["client"] = recovered
        release_resolver.set()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [{"ok": True, "position": 1, "name": "song"}])
        queue_key = area_key(QUEUE, "area-A")
        self.assertEqual(retired.llen(queue_key), 0)
        self.assertEqual(retired.llen(WEB_COMMANDS), 0)
        self.assertEqual(recovered.llen(queue_key), 1)
        self.assertEqual(recovered.llen(WEB_COMMANDS), 1)

    def test_concurrent_add_song_returns_distinct_atomic_positions(self) -> None:
        from app.services.playback.control_service import PlaybackControlService
        from core.redis_keys import QUEUE, WEB_COMMANDS, area_key
        from domain.playback import decode_web_command

        queue_key = area_key(QUEUE, "area-A")
        atomic_entry_barrier = threading.Barrier(2)
        hook_calls: list[str] = []
        hook_lock = threading.Lock()

        class ConcurrentMemoryRedis(qm._InMemoryRedis):
            def enqueue_song_and_notify(
                self,
                queue_key: str,
                song: object,
                commands_key: str,
                notification_template: str,
            ) -> int:
                with hook_lock:
                    hook_calls.append(str(song))
                atomic_entry_barrier.wait(timeout=2)
                return super().enqueue_song_and_notify(
                    queue_key,
                    song,
                    commands_key,
                    notification_template,
                )

        client = ConcurrentMemoryRedis()
        platform = Mock(name="Platform")
        platform.get_song_url.return_value = "https://example.test/song.mp3"
        service = PlaybackControlService(
            client,
            platform_resolver=lambda _name: platform,
        )
        results: list[dict[str, object]] = []

        def add_song(song_id: str) -> None:
            results.append(
                service.add_song(
                    area="area-A",
                    body={
                        "id": song_id,
                        "name": f"song-{song_id}",
                        "artists": "artist",
                    },
                )
            )

        workers = [
            threading.Thread(target=add_song, args=(song_id,))
            for song_id in ("1", "2")
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(hook_calls), 2, "两个请求都必须经过原子入队入口")
        positions: list[int] = []
        for result in results:
            position = result.get("position")
            self.assertIsInstance(position, int)
            if isinstance(position, int):
                positions.append(position)
        self.assertEqual(sorted(positions), [1, 2])
        self.assertEqual(client.llen(queue_key), 2)
        self.assertEqual(client.llen(WEB_COMMANDS), 2)
        queued = [json.loads(str(raw)) for raw in client.lrange(queue_key, 0, -1)]
        notifications = [
            decode_web_command(str(raw))
            for raw in client.lrange(WEB_COMMANDS, 0, -1)
        ]
        self.assertEqual(
            [command.payload["position"] for command in notifications],
            [1, 2],
        )
        self.assertEqual(
            [command.payload["name"] for command in notifications],
            [song["name"] for song in queued],
        )

    def test_memory_wrong_type_prevalidation_prevents_partial_queue_write(self) -> None:
        from app.services.playback.control_service import PlaybackControlService
        from core.redis_keys import QUEUE, WEB_COMMANDS, area_key

        client = qm._InMemoryRedis()
        client.set(WEB_COMMANDS, "not-a-list")
        platform = Mock(name="Platform")
        platform.get_song_url.return_value = "https://example.test/song.mp3"
        service = PlaybackControlService(
            client,
            platform_resolver=lambda _name: platform,
        )

        with self.assertRaisesRegex(TypeError, "Web 命令键"):
            service.add_song(
                area="area-A",
                body={"id": "1", "name": "song", "artists": "artist"},
            )

        self.assertEqual(client.llen(area_key(QUEUE, "area-A")), 0)
        self.assertEqual(client.get(WEB_COMMANDS), "not-a-list")


class QueueManagerAppendPositionTest(unittest.TestCase):
    def test_add_to_queue_uses_rpush_return_value_directly(self) -> None:
        client = Mock(name="Redis")
        client.rpush.return_value = 3
        with patch.object(qm, "get_redis_client", return_value=client):
            queue = qm.QueueManager(area="area-A")
            position = queue.add_to_queue({"name": "song"})

        self.assertEqual(position, 2)
        client.rpush.assert_called_once()
        client.pipeline.assert_not_called()


class RealRedisAtomicEnqueueTest(unittest.TestCase):
    """CI 提供 Redis；本地无 Redis 时只跳过真实 Lua 集成层。"""

    def setUp(self) -> None:
        self.client: Any = qm.redis.Redis(**dict(qm.REDIS_CONFIG))
        try:
            self.client.ping()
        except Exception as exc:
            self.client.close()
            self.skipTest(f"Redis unavailable: {exc}")
        prefix = f"oopzbot:test:{uuid.uuid4().hex}"
        self.queue_key = f"{prefix}:queue"
        self.commands_key = f"{prefix}:commands"

    def tearDown(self) -> None:
        if hasattr(self, "client"):
            self.client.delete(self.queue_key, self.commands_key)
            self.client.close()

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

    def test_lua_serializes_queue_position_and_notification_order(self) -> None:
        from core.queue_manager import atomic_enqueue_song_and_notify
        from domain.playback import decode_web_command

        start = threading.Barrier(3)
        results: list[int] = []

        def enqueue(name: str) -> None:
            start.wait(timeout=2)
            results.append(
                atomic_enqueue_song_and_notify(
                    self.client,
                    self.queue_key,
                    json.dumps({"name": name}),
                    self.commands_key,
                    self._notification_template(name),
                )
            )

        workers = [
            threading.Thread(target=enqueue, args=(name,))
            for name in ("song-1", "song-2")
        ]
        for worker in workers:
            worker.start()
        start.wait(timeout=2)
        for worker in workers:
            worker.join(timeout=2)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(sorted(results), [1, 2])
        queued = [json.loads(raw) for raw in self.client.lrange(self.queue_key, 0, -1)]
        notifications = [
            decode_web_command(raw)
            for raw in self.client.lrange(self.commands_key, 0, -1)
        ]
        self.assertEqual(
            [command.payload["position"] for command in notifications],
            [1, 2],
        )
        self.assertEqual(
            [command.payload["name"] for command in notifications],
            [song["name"] for song in queued],
        )

    def test_lua_prevalidates_both_key_types_before_any_write(self) -> None:
        from core.queue_manager import atomic_enqueue_song_and_notify

        self.client.set(self.queue_key, "not-a-list")

        with self.assertRaises(ResponseError):
            atomic_enqueue_song_and_notify(
                self.client,
                self.queue_key,
                json.dumps({"name": "song"}),
                self.commands_key,
                self._notification_template("song"),
            )

        self.assertEqual(self.client.get(self.queue_key), "not-a-list")
        self.assertEqual(self.client.llen(self.commands_key), 0)

        self.client.delete(self.queue_key, self.commands_key)
        self.client.set(self.commands_key, "not-a-list")

        with self.assertRaises(ResponseError):
            atomic_enqueue_song_and_notify(
                self.client,
                self.queue_key,
                json.dumps({"name": "song"}),
                self.commands_key,
                self._notification_template("song"),
            )

        self.assertEqual(self.client.llen(self.queue_key), 0)
        self.assertEqual(self.client.get(self.commands_key), "not-a-list")


class ConversationMemoryProviderTest(unittest.TestCase):
    """ConversationMemory 应通过 provider 现取客户端，跟随 Redis 恢复。"""

    def test_memory_follows_provider_swap(self) -> None:
        from services.conversation_memory import ConversationMemory

        first = Mock(name="MemoryClient")
        first.get = Mock(return_value=None)
        second = Mock(name="RealClient")
        second.get = Mock(return_value=None)
        current = {"client": first}

        memory = ConversationMemory(lambda: current["client"], max_rounds=5)

        memory.get_history("user-1", "channel-1")
        first.get.assert_called_once()

        current["client"] = second
        memory.get_history("user-1", "channel-1")
        second.get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
