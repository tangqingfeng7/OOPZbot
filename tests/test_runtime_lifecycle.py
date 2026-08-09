"""运行时服务关停顺序、幂等性和停止后无轮询的契约测试。"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


class ShutdownCoordinatorTest(unittest.TestCase):
    def test_shutdown_order_keeps_dependencies_alive_during_dispatcher_drain(self) -> None:
        from app.lifecycle.shutdown_coordinator import ShutdownCoordinator
        from core.database import MessageStatsDB

        events: list[str] = []
        netease = mock.Mock()
        voice = mock.Mock()
        dispatcher = mock.Mock()
        background = mock.Mock()

        background.stop_ingress.side_effect = lambda *, timeout: events.append("ingress")
        background.stop_producers.side_effect = lambda *, timeout: events.append("producers")
        background.stop_plugins.side_effect = lambda *, timeout: events.append("plugins")

        def stop_dispatcher(*, timeout: float) -> bool:
            self.assertLessEqual(timeout, ShutdownCoordinator.DISPATCHER_DRAIN_SECONDS)
            netease.stop.assert_not_called()
            voice.destroy.assert_not_called()
            background.stop_plugins.assert_not_called()
            events.append("dispatcher")
            return True

        dispatcher.stop.side_effect = stop_dispatcher
        netease.stop.side_effect = lambda *, timeout: events.append("netease")
        voice.destroy.side_effect = lambda *, timeout: events.append("voice")
        context: Any = SimpleNamespace(dispatcher=dispatcher, voice=voice)

        coordinator = ShutdownCoordinator()
        with mock.patch.object(
            MessageStatsDB,
            "stop",
            side_effect=lambda *, timeout: events.append("database"),
        ):
            coordinator.stop(context, netease, background)
            coordinator.stop(context, netease, background)

        self.assertEqual(
            events,
            [
                "ingress",
                "producers",
                "dispatcher",
                "plugins",
                "database",
                "netease",
                "voice",
            ],
        )
        background.stop_ingress.assert_called_once()
        background.stop_producers.assert_called_once()
        background.stop_plugins.assert_called_once()
        dispatcher.stop.assert_called_once()
        netease.stop.assert_called_once()
        voice.destroy.assert_called_once()

    def test_dispatcher_drains_plugin_command_before_registry_unload(self) -> None:
        from app.infrastructure.runtime import PluginRuntime
        from app.lifecycle.background_services import BackgroundServiceRunner
        from app.lifecycle.shutdown_coordinator import ShutdownCoordinator
        from core.database import MessageStatsDB
        from core.message_dispatcher import MessageDispatcher
        from domain.plugins.base import (
            BotModule,
            PluginCommandCapabilities,
            PluginMetadata,
        )

        events: list[str] = []
        dispatch_results: list[bool] = []
        blocker_entered = threading.Event()
        release_blocker = threading.Event()
        drain_started = threading.Event()

        class QueuedPlugin(BotModule):
            @property
            def metadata(self) -> PluginMetadata:
                return PluginMetadata(name="queued", description="queued command")

            @property
            def command_capabilities(self) -> PluginCommandCapabilities:
                return PluginCommandCapabilities(mention_prefixes=("queued",))

            def handle_mention(
                self,
                text: str,
                channel: str,
                area: str,
                user: str,
                handler: Any,
            ) -> bool:
                events.append("handled")
                return True

            def on_unload(self) -> None:
                events.append("unloaded")

        plugins = PluginRuntime()
        plugins.registry.register(QueuedPlugin())
        dispatcher = MessageDispatcher(workers=1, maxsize=4)

        def block_worker() -> None:
            blocker_entered.set()
            release_blocker.wait(timeout=2)

        dispatcher.submit("same-shard", block_worker)
        dispatcher.submit(
            "same-shard",
            lambda: dispatch_results.append(
                plugins.try_dispatch_mention(
                    "queued command",
                    "channel",
                    "area",
                    "user",
                    None,
                )
            ),
        )
        self.assertTrue(blocker_entered.wait(timeout=1))

        handler = SimpleNamespace(
            infrastructure=SimpleNamespace(music=mock.Mock(), plugins=plugins),
            services=SimpleNamespace(
                scheduler=SimpleNamespace(
                    scheduled=mock.Mock(),
                    reminder=mock.Mock(),
                ),
                safety=SimpleNamespace(recall_scheduler=mock.Mock()),
            ),
        )
        context: Any = SimpleNamespace(
            client=mock.Mock(),
            onebot_v11=None,
            notifier_callback=None,
            handler=handler,
            dispatcher=dispatcher,
            voice=None,
        )
        background = BackgroundServiceRunner()
        background._context = context
        netease = mock.Mock()
        coordinator = ShutdownCoordinator()
        real_dispatcher_stop = dispatcher.stop

        def stop_dispatcher(*, timeout: float) -> bool:
            drain_started.set()
            return real_dispatcher_stop(timeout=timeout)

        shutdown_thread = threading.Thread(
            target=coordinator.stop,
            args=(context, netease, background),
            daemon=True,
        )
        try:
            with (
                mock.patch.object(dispatcher, "stop", side_effect=stop_dispatcher),
                mock.patch.object(MessageStatsDB, "stop"),
            ):
                shutdown_thread.start()
                self.assertTrue(drain_started.wait(timeout=1))
                release_blocker.set()
                shutdown_thread.join(timeout=2)
        finally:
            release_blocker.set()
            shutdown_thread.join(timeout=2)

        self.assertFalse(shutdown_thread.is_alive())
        self.assertEqual(dispatch_results, [True])
        self.assertEqual(events, ["handled", "unloaded"])
        self.assertEqual(plugins.enabled_plugin_names(), [])

    def test_dispatcher_timeout_skips_plugin_unload_until_worker_exits(self) -> None:
        from app.infrastructure.runtime import PluginRuntime
        from app.lifecycle.background_services import BackgroundServiceRunner
        from app.lifecycle.shutdown_coordinator import ShutdownCoordinator
        from core.database import MessageStatsDB
        from core.message_dispatcher import MessageDispatcher
        from domain.plugins.base import (
            BotModule,
            PluginCommandCapabilities,
            PluginMetadata,
        )

        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        unloaded = threading.Event()

        class BlockingPlugin(BotModule):
            @property
            def metadata(self) -> PluginMetadata:
                return PluginMetadata(name="blocking", description="blocking command")

            @property
            def command_capabilities(self) -> PluginCommandCapabilities:
                return PluginCommandCapabilities(mention_prefixes=("blocking",))

            def handle_mention(
                self,
                text: str,
                channel: str,
                area: str,
                user: str,
                handler: Any,
            ) -> bool:
                entered.set()
                release.wait(timeout=2)
                finished.set()
                return True

            def on_unload(self) -> None:
                unloaded.set()

        plugins = PluginRuntime()
        plugins.registry.register(BlockingPlugin())
        dispatcher = MessageDispatcher(workers=1, maxsize=4)
        self.assertTrue(
            dispatcher.submit(
                "plugin-command",
                plugins.try_dispatch_mention,
                "blocking command",
                "channel",
                "area",
                "user",
                None,
            )
        )
        self.assertTrue(entered.wait(timeout=1))

        handler = SimpleNamespace(
            infrastructure=SimpleNamespace(music=mock.Mock(), plugins=plugins),
            services=SimpleNamespace(
                scheduler=SimpleNamespace(
                    scheduled=mock.Mock(),
                    reminder=mock.Mock(),
                ),
                safety=SimpleNamespace(recall_scheduler=mock.Mock()),
            ),
        )
        context: Any = SimpleNamespace(
            client=mock.Mock(),
            onebot_v11=None,
            notifier_callback=None,
            handler=handler,
            dispatcher=dispatcher,
            voice=None,
        )
        background = BackgroundServiceRunner()
        background._context = context
        netease = mock.Mock()
        coordinator = ShutdownCoordinator()
        coordinator.DISPATCHER_DRAIN_SECONDS = 0.03

        try:
            with (
                mock.patch.object(MessageStatsDB, "stop"),
                mock.patch("app.lifecycle.shutdown_coordinator.logger.warning") as warning,
            ):
                coordinator.stop(context, netease, background)

            self.assertFalse(finished.is_set(), "关停返回时插件命令仍应处于 in-flight")
            self.assertFalse(unloaded.is_set(), "不得与 in-flight 插件命令并发卸载")
            self.assertEqual(plugins.enabled_plugin_names(), ["blocking"])
            self.assertTrue(
                any("跳过插件卸载" in call.args[0] for call in warning.call_args_list),
                "跳过插件卸载必须留下明确告警",
            )
            netease.stop.assert_called_once()

            release.set()
            self.assertTrue(dispatcher.stop(timeout=1))
            self.assertTrue(finished.is_set())
            self.assertFalse(unloaded.is_set())
        finally:
            release.set()
            dispatcher.stop(timeout=1)
            plugins.stop(timeout=1)

        self.assertTrue(unloaded.is_set())


class BackgroundServiceRunnerIsolationTest(unittest.TestCase):
    def test_stop_ingress_continues_after_one_service_fails(self) -> None:
        from app.lifecycle.background_services import BackgroundServiceRunner

        client = mock.Mock()
        client.stop.side_effect = RuntimeError("client stop failed")
        onebot_v11 = mock.Mock()
        web_player = mock.Mock()
        runner = BackgroundServiceRunner()
        context: Any = SimpleNamespace(client=client, onebot_v11=onebot_v11)
        runner._context = context
        runner._web_player = web_player

        runner.stop_ingress(timeout=1)

        client.stop.assert_called_once()
        onebot_v11.stop.assert_called_once()
        web_player.stop.assert_called_once()

    def test_stop_producers_continues_after_each_service_fails(self) -> None:
        from app.lifecycle.background_services import BackgroundServiceRunner

        music = mock.Mock()
        music.stop.side_effect = RuntimeError("music stop failed")
        notifier = mock.Mock()
        scheduled = mock.Mock()
        scheduled.stop.side_effect = RuntimeError("scheduled stop failed")
        reminder = mock.Mock()
        recall = mock.Mock()
        plugins = mock.Mock()
        context: Any = SimpleNamespace(
            handler=SimpleNamespace(
                infrastructure=SimpleNamespace(music=music, plugins=plugins),
                services=SimpleNamespace(
                    scheduler=SimpleNamespace(
                        scheduled=scheduled,
                        reminder=reminder,
                    ),
                    safety=SimpleNamespace(recall_scheduler=recall),
                ),
            ),
            notifier_callback=notifier,
        )
        runner = BackgroundServiceRunner()
        runner._context = context

        runner.stop_producers(timeout=1)

        music.stop.assert_called_once()
        notifier.stop.assert_called_once()
        scheduled.stop.assert_called_once()
        reminder.stop.assert_called_once()
        recall.stop.assert_called_once()
        plugins.stop.assert_not_called()

        runner.stop_plugins(timeout=1)
        plugins.stop.assert_called_once()


class MessageStatsLifecycleTest(unittest.TestCase):
    def test_final_flush_does_not_exceed_remaining_stop_budget(self) -> None:
        from core.database import _MessageStatsBatcher

        batcher = _MessageStatsBatcher()
        batcher._buffer[("2026-08-02", "channel", "area", "user")] = 1
        entered = threading.Event()
        release = threading.Event()

        def blocking_flush() -> None:
            entered.set()
            release.wait(timeout=1)

        with mock.patch.object(batcher, "flush", side_effect=blocking_flush):
            started_at = time.monotonic()
            batcher.stop(timeout=0.03)
            elapsed = time.monotonic() - started_at
            self.assertTrue(entered.is_set())
            self.assertLess(elapsed, 0.15)
            release.set()
            if batcher._final_flush_thread:
                batcher._final_flush_thread.join(timeout=1)

    def test_zero_budget_starts_best_effort_flush_without_blocking(self) -> None:
        from core.database import _MessageStatsBatcher

        batcher = _MessageStatsBatcher()
        entered = threading.Event()
        release = threading.Event()

        def blocking_flush() -> None:
            entered.set()
            release.wait(timeout=1)

        with mock.patch.object(batcher, "flush", side_effect=blocking_flush):
            batcher.increment("2026-08-02", "channel", "area", "user")
            started_at = time.monotonic()
            batcher.stop(timeout=0)
            elapsed = time.monotonic() - started_at
            self.assertLess(elapsed, 0.05)
            self.assertTrue(entered.wait(timeout=1))
            self.assertIsNotNone(batcher._final_flush_thread)
            release.set()
            if batcher._thread:
                batcher._thread.join(timeout=1)
            if batcher._final_flush_thread:
                batcher._final_flush_thread.join(timeout=1)


class MusicListenerLifecycleTest(unittest.TestCase):
    def test_stop_is_idempotent_and_prevents_further_blpop_calls(self) -> None:
        import music.music as music_module

        class BlockingRedis:
            def __init__(self) -> None:
                self.calls = 0
                self.entered = threading.Event()
                self._lock = threading.Lock()

            def blpop(self, _key: str, timeout: int):
                with self._lock:
                    self.calls += 1
                self.entered.set()
                time.sleep(min(0.02, timeout))
                return None

            def call_count(self) -> int:
                with self._lock:
                    return self.calls

        redis_client = BlockingRedis()
        handler = music_module.MusicHandler.__new__(music_module.MusicHandler)
        handler._service_stop_event = threading.Event()
        handler._auto_play_thread = None
        handler._web_command_thread = None

        with mock.patch.object(
            music_module,
            "get_redis_client",
            return_value=redis_client,
        ):
            handler.start_web_command_listener()
            self.assertTrue(redis_client.entered.wait(timeout=1))
            handler.stop(timeout=1)
            calls_after_stop = redis_client.call_count()
            handler.stop(timeout=1)
            time.sleep(0.05)

        self.assertEqual(redis_client.call_count(), calls_after_stop)


class AreaNotifierLifecycleTest(unittest.TestCase):
    def test_notifier_stop_wakes_and_joins_poll_thread(self) -> None:
        import services.area_join_notifier as notifier_module

        stopped = threading.Event()

        def poll_loop(*_args, stop_event: threading.Event, **_kwargs) -> None:
            stop_event.wait(timeout=2)
            stopped.set()

        with mock.patch.object(notifier_module, "_run_join_poll_loop", poll_loop):
            notifier = notifier_module.AreaJoinNotifier(lambda *_args: None, (), {})
            notifier.start()
            notifier.stop(timeout=1)
            notifier.stop(timeout=1)

        self.assertTrue(stopped.is_set())
        self.assertFalse(notifier._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
