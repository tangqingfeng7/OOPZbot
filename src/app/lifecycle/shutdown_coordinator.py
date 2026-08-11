"""应用级异步、有界且幂等的关停顺序。"""

from __future__ import annotations

import asyncio

from app.lifecycle.background_services import BackgroundServiceRunner
from app.lifecycle.context import AppContext
from app.lifecycle.netease_api_runtime import NeteaseApiRuntime
from app.lifecycle.task_supervisor import TaskSupervisor
from core.logger_config import setup_logger

logger = setup_logger("ShutdownCoordinator")


class ShutdownCoordinator:
    TOTAL_BUDGET_SECONDS = 20.0
    DISPATCHER_DRAIN_SECONDS = 8.0

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._stopped = False

    async def stop(
        self,
        context: AppContext | None,
        netease_runtime: NeteaseApiRuntime,
        background_services: BackgroundServiceRunner | None = None,
        supervisor: TaskSupervisor | None = None,
    ) -> None:
        async with self._lock:
            if self._stopped:
                return
            self._stopped = True

        try:
            await asyncio.wait_for(
                self._stop_in_order(
                    context,
                    netease_runtime,
                    background_services,
                    supervisor,
                ),
                timeout=self.TOTAL_BUDGET_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("应用关停已用尽 %.0f 秒总预算", self.TOTAL_BUDGET_SECONDS)

    async def _stop_in_order(
        self,
        context: AppContext | None,
        netease_runtime: NeteaseApiRuntime,
        background_services: BackgroundServiceRunner | None,
        supervisor: TaskSupervisor | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        deadline = started_at + self.TOTAL_BUDGET_SECONDS

        def remaining(cap: float | None = None) -> float:
            value = max(0.0, deadline - loop.time())
            return min(value, cap) if cap is not None else value

        async def attempt(name: str, awaitable) -> None:
            try:
                await awaitable
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("停止 %s 时出现异常: %s", name, exc)

        if background_services is not None:
            await attempt(
                "外部入口",
                background_services.stop_ingress(timeout=remaining(5.0)),
            )

        if context is not None:
            await attempt("Oopz-SDK", context.client.stop())

        if background_services is not None:
            await attempt(
                "后台生产者",
                background_services.stop_producers(timeout=remaining(5.0)),
            )

        dispatcher_stopped = True
        if context is not None and context.dispatcher is not None:
            try:
                dispatcher_stopped = await context.dispatcher.stop(
                    timeout=remaining(self.DISPATCHER_DRAIN_SECONDS)
                )
            except Exception as exc:
                dispatcher_stopped = False
                logger.warning("停止消息分发器时出现异常: %s", exc)

        if not dispatcher_stopped:
            logger.warning("消息分发器未完全停止，跳过插件卸载以避免并发清空注册表")
        elif background_services is not None:
            await attempt(
                "插件",
                background_services.stop_plugins(timeout=remaining(5.0)),
            )

        if context is not None:
            await attempt("AI HTTP 会话", context.handler.infrastructure.chat.close())
            from oopz.name_resolver import get_resolver

            await attempt("名称缓存", get_resolver().close())

        from core.database import MessageStatsDB

        await attempt(
            "消息统计缓冲区",
            MessageStatsDB.stop(remaining(3.0)),
        )
        await attempt("网易云 API", netease_runtime.stop(timeout=remaining(5.0)))

        effective_supervisor = supervisor or (context.supervisor if context else None)
        if effective_supervisor is not None:
            await attempt(
                "后台任务",
                effective_supervisor.close(timeout=remaining()),
            )

        logger.info("应用运行时已按顺序停止（%.2fs）", loop.time() - started_at)
