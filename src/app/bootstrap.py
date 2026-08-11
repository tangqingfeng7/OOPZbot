from __future__ import annotations

import asyncio
import signal

from app.lifecycle import (
    AppContext,
    AppContextBuilder,
    BackgroundServiceRunner,
    NeteaseApiRuntime,
    ShutdownCoordinator,
    StartupResourceBuilder,
    TaskSupervisor,
)
from core.logger_config import setup_logger

logger = setup_logger("Main")


class BotApplication:
    """在一个 asyncio 事件循环内组装、运行并关闭 Bot。"""

    def __init__(self) -> None:
        self._netease_runtime = NeteaseApiRuntime()
        self._background_services = BackgroundServiceRunner()
        self._context_builder = AppContextBuilder()
        self._shutdown = ShutdownCoordinator()
        self._startup_resources = StartupResourceBuilder()
        self._supervisor = TaskSupervisor()
        self._context: AppContext | None = None
        self._stop_event = asyncio.Event()
        self._shutdown_in_progress = False
        self._stop_signal: signal.Signals | None = None

    @staticmethod
    def _warn_if_no_admins() -> None:
        from config import ADMIN_UIDS

        if ADMIN_UIDS:
            return
        logger.warning("=" * 50)
        logger.warning("未配置 ADMIN_UIDS，所有管理命令对任何人都不可用。")
        logger.warning("在频道里发送 /setup 可查看你的 UID 与配置步骤。")
        logger.warning("=" * 50)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()

        def request_stop(sig: signal.Signals) -> None:
            if self._stop_signal is None:
                self._stop_signal = sig
            self._stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, request_stop, sig)
            except (NotImplementedError, RuntimeError):
                signal.signal(
                    sig,
                    lambda _signum, _frame, value=sig: loop.call_soon_threadsafe(
                        request_stop,
                        value,
                    ),
                )

    async def run(self) -> None:
        logger.info("=" * 50)
        logger.info("Oopz Bot 正在启动...")
        logger.info("=" * 50)
        self._warn_if_no_admins()
        self._install_signal_handlers()

        failure_waiter: asyncio.Task | None = None
        stop_waiter: asyncio.Task | None = None
        try:
            await self._startup_resources.build()
            await self._netease_runtime.start()
            self._context = await self._context_builder.build(self._supervisor)
            await self._background_services.start(self._context)

            failure_waiter = asyncio.create_task(
                self._supervisor.wait_failure(),
                name="application-failure-waiter",
            )
            stop_waiter = asyncio.create_task(
                self._stop_event.wait(),
                name="application-stop-waiter",
            )
            done, _pending = await asyncio.wait(
                {failure_waiter, stop_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if failure_waiter in done:
                raise await failure_waiter
        finally:
            for waiter in (failure_waiter, stop_waiter):
                if waiter is not None and not waiter.done():
                    waiter.cancel()
            await asyncio.gather(
                *(waiter for waiter in (failure_waiter, stop_waiter) if waiter is not None),
                return_exceptions=True,
            )
            await self.stop()

        logger.info("Oopz Bot 已停止。")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._shutdown_in_progress:
            return
        self._shutdown_in_progress = True
        if self._stop_signal is not None:
            logger.info("收到 %s，正在停止...", self._stop_signal.name)
        await self._shutdown.stop(
            self._context,
            self._netease_runtime,
            self._background_services,
            self._supervisor,
        )
