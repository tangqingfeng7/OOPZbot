"""基于 asyncio 的有界多分片消息分发器。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from core.logger_config import get_logger

logger = get_logger("MessageDispatcher")
_STOP = object()


class MessageDispatcher:
    """按 key 固定分片，保证单频道顺序并隔离不同频道。"""

    def __init__(self, workers: int = 4, maxsize: int = 512, name: str = "MsgWorker"):
        self._worker_count = max(1, int(workers))
        self._queues: list[asyncio.Queue[tuple[object, tuple[Any, ...]]]] = [
            asyncio.Queue(maxsize=max(1, int(maxsize)))
            for _ in range(self._worker_count)
        ]
        self._workers: list[asyncio.Task[None]] = []
        self._name = name
        self._started = False
        self._stopping = False
        self._dropped = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped

    @property
    def pending_count(self) -> int:
        return sum(queue.qsize() for queue in self._queues)

    def start(self) -> None:
        """在当前事件循环创建固定数量的 worker task。"""
        if self._started or self._stopping:
            return
        loop = asyncio.get_running_loop()
        self._started = True
        self._workers = [
            loop.create_task(self._worker_loop(queue), name=f"{self._name}-{index}")
            for index, queue in enumerate(self._queues)
        ]

    def submit(self, key: str, fn: Callable[..., Any], *args: Any) -> bool:
        """非阻塞入队；满载或关停时返回 False。"""
        if self._stopping:
            return False
        self.start()
        shard = self._queues[hash(key) % self._worker_count]
        try:
            shard.put_nowait((fn, args))
            return True
        except asyncio.QueueFull:
            self._dropped += 1
            logger.warning(
                "消息处理队列已满，丢弃一条消息 (key=%s, 累计丢弃 %d)",
                key,
                self._dropped,
            )
            return False

    async def stop(self, timeout: float = 5.0) -> bool:
        """在总预算内排空队列并停止 worker；停过的实例不可重启。"""
        self._stopping = True
        if not self._workers:
            self._started = False
            return True

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        joins = [asyncio.create_task(queue.join()) for queue in self._queues]
        try:
            await asyncio.wait_for(
                asyncio.gather(*joins),
                timeout=max(0.0, deadline - loop.time()),
            )
        except asyncio.TimeoutError:
            cancelled = self._cancel_waiting_tasks()
            logger.warning("关停超时，取消 %d 条尚未开始的消息", cancelled)
        finally:
            for task in joins:
                if not task.done():
                    task.cancel()

        for queue in self._queues:
            queue.put_nowait((_STOP, ()))

        done, pending = await asyncio.wait(
            self._workers,
            timeout=max(0.0, deadline - loop.time()),
        )
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                logger.error("消息 worker 异常退出", exc_info=task.exception())
        for task in pending:
            logger.warning("服务停止超时: %s，任务仍未退出", task.get_name())
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        self._workers = []
        self._started = False
        return not pending

    def _cancel_waiting_tasks(self) -> int:
        cancelled = 0
        for queue in self._queues:
            while True:
                try:
                    fn, _args = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                queue.task_done()
                if fn is _STOP:
                    queue.put_nowait((_STOP, ()))
                    break
                cancelled += 1
        return cancelled

    async def _worker_loop(self, shard: asyncio.Queue) -> None:
        while True:
            fn, args = await shard.get()
            try:
                if fn is _STOP:
                    return
                result = fn(*args)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("消息处理任务异常")
            finally:
                shard.task_done()
