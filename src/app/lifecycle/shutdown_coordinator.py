import threading
import time

from app.lifecycle.background_services import BackgroundServiceRunner
from app.lifecycle.context import AppContext
from app.lifecycle.netease_api_runtime import NeteaseApiRuntime
from core.logger_config import setup_logger

logger = setup_logger("ShutdownCoordinator")


class ShutdownCoordinator:
    """负责关闭应用运行时资源。"""

    TOTAL_BUDGET_SECONDS = 20.0
    DISPATCHER_DRAIN_SECONDS = 8.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stopped = False

    def stop(
        self,
        context: AppContext | None,
        netease_runtime: NeteaseApiRuntime,
        background_services: BackgroundServiceRunner | None = None,
    ) -> None:
        """按依赖顺序在 20 秒总预算内幂等关停。"""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True

        started_at = time.monotonic()
        deadline = started_at + self.TOTAL_BUDGET_SECONDS

        def remaining(cap: float | None = None) -> float:
            value = max(0.0, deadline - time.monotonic())
            return min(value, cap) if cap is not None else value

        def attempt(name: str, callback) -> None:
            try:
                callback()
            except Exception as exc:
                logger.warning("停止 %s 时出现异常: %s", name, exc)

        # 1. 先关闭所有入口，确保 dispatcher 不再持续收到新任务。
        if background_services is not None:
            attempt(
                "外部入口",
                lambda: background_services.stop_ingress(timeout=remaining()),
            )
        elif context:
            attempt("Oopz", lambda: context.client.stop(timeout=remaining()))
            onebot_v11 = context.onebot_v11
            if onebot_v11:
                attempt("OneBot v11", lambda: onebot_v11.stop(timeout=remaining()))

        # 2. 再停止所有轮询、监听与调度生产者。
        if background_services is not None:
            attempt(
                "后台生产者",
                lambda: background_services.stop_producers(timeout=remaining()),
            )
        elif context:
            music = context.handler.infrastructure.music
            attempt("音乐后台服务", lambda: music.stop(timeout=remaining()))
            notifier = context.notifier_callback
            if notifier and hasattr(notifier, "stop"):
                attempt("区域通知", lambda: notifier.stop(timeout=remaining()))
            scheduler = context.handler.services.scheduler
            attempt("定时消息", lambda: scheduler.scheduled.stop(timeout=remaining()))
            attempt("提醒", lambda: scheduler.reminder.stop(timeout=remaining()))
            attempt(
                "自动撤回",
                lambda: context.handler.services.safety.recall_scheduler.stop(timeout=remaining()),
            )
        # 3. 网易云、sender 与插件注册表仍存活时，给已接收消息最多 8 秒完成
        # 业务处理。插件必须留到 dispatcher 排空后再卸载，否则已经入队的
        # 插件命令会因 registry 被提前清空而静默丢失。
        dispatcher_fully_stopped = True
        if context and context.dispatcher:
            dispatcher = context.dispatcher
            try:
                dispatcher_fully_stopped = (
                    dispatcher.stop(timeout=remaining(self.DISPATCHER_DRAIN_SECONDS)) is True
                )
            except Exception as exc:
                dispatcher_fully_stopped = False
                logger.warning("停止消息分发器时出现异常: %s", exc)

        # 4. 已入队命令处理完成后再卸载插件，并隔离卸载异常/超时。
        # 如果 dispatcher 仍有 in-flight worker，不能在其回调执行期间并发
        # 清空插件 registry；此时跳过卸载，daemon worker 由进程退出兜底。
        if not dispatcher_fully_stopped:
            logger.warning(
                "消息分发器未在预算内完全停止，跳过插件卸载，避免与仍在执行的插件命令并发"
            )
        elif background_services is not None:
            attempt(
                "插件",
                lambda: background_services.stop_plugins(timeout=remaining()),
            )
        elif context:
            attempt(
                "插件",
                lambda: context.handler.infrastructure.plugins.stop(timeout=remaining()),
            )

        # 5. 刷新数据库缓冲。
        def _flush_database() -> None:
            from core.database import MessageStatsDB

            MessageStatsDB.stop(timeout=remaining())

        attempt("消息统计缓冲区", _flush_database)

        # 6. 所有可能依赖网易云的任务完成后再停止子进程。
        attempt("网易云 API", lambda: netease_runtime.stop(timeout=remaining()))

        # 7. 最后销毁语音客户端。
        if context and context.voice:
            voice = context.voice
            attempt("语音客户端", lambda: voice.destroy(timeout=remaining()))

        elapsed = time.monotonic() - started_at
        if elapsed >= self.TOTAL_BUDGET_SECONDS:
            logger.warning("应用关停已用尽 %.0f 秒总预算", self.TOTAL_BUDGET_SECONDS)
        else:
            logger.info("应用运行时已按顺序停止（%.2fs）", elapsed)
