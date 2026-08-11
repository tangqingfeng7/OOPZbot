"""插件共享的可取消异步间隔任务基类。

统一封装后台任务的「启动一次 / 停止 / 间隔轮询」生命周期，避免各推送任务

"""

from __future__ import annotations

import asyncio
import contextlib

from core.logger_config import get_logger


class IntervalWorker:
    """按固定间隔运行 ``_tick`` 的异步任务基类。

    任务以 ``_stop_event.wait(interval)`` 驱动。``_tick`` 抛出的异常会被记录，
    取消则立即向上传播并退出。
    """

    _NAME = "IntervalWorker"

    def __init__(self, interval: int) -> None:
        self._interval = max(1, int(interval))
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._logger = get_logger(self._NAME)

    @property
    def stopping(self) -> bool:
        """供 ``_tick`` 在长循环中检查是否应提前退出。"""
        return self._stop_event.is_set()

    def _start_task(self) -> bool:
        """启动后台任务；若已在运行返回 False（幂等）。"""
        if self._task is not None and not self._task.done():
            return False
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name=self._NAME)
        return True

    async def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=max(0.0, timeout))

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("%s: tick failed", self._NAME)

    async def _tick(self) -> None:
        raise NotImplementedError
