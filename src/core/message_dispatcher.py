"""有界多分片消息分发器：把 WS 回调中的业务处理移出接收线程。

WebSocket 接收线程只负责解析和入队；真正的命令处理（可能包含 AI 请求、
音乐搜索、成员分页查询等慢操作）由固定数量的工作线程消费执行。

路由规则：同一 key（通常是 ``area:channel``）恒定映射到同一个工作线程，
保证单频道内消息按到达顺序处理；不同频道之间互不阻塞。

背压策略：分片队列有界，队列满时丢弃新消息并告警，绝不把阻塞传导回
WS 接收线程——对聊天机器人而言，过载时丢一条消息比整体失联好。
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable

from core.logger_config import get_logger

logger = get_logger("MessageDispatcher")

_STOP = object()


class MessageDispatcher:
    def __init__(self, workers: int = 4, maxsize: int = 512, name: str = "MsgWorker"):
        self._worker_count = max(1, int(workers))
        self._queues: list[queue.Queue] = [
            queue.Queue(maxsize=max(1, int(maxsize))) for _ in range(self._worker_count)
        ]
        self._threads: list[threading.Thread] = []
        self._name = name
        self._started = False
        self._state_lock = threading.Lock()
        self._dropped = 0
        self._dropped_lock = threading.Lock()

    @property
    def dropped_count(self) -> int:
        return self._dropped

    def start(self) -> None:
        with self._state_lock:
            if self._started:
                return
            self._started = True
            for index, shard in enumerate(self._queues):
                thread = threading.Thread(
                    target=self._worker_loop,
                    args=(shard,),
                    name=f"{self._name}-{index}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)

    def submit(self, key: str, fn: Callable[..., Any], *args: Any) -> bool:
        """按 key 分片入队。返回 False 表示队列已满、消息被丢弃。"""
        if not self._started:
            self.start()
        shard = self._queues[hash(key) % self._worker_count]
        try:
            shard.put_nowait((fn, args))
            return True
        except queue.Full:
            with self._dropped_lock:
                self._dropped += 1
                dropped = self._dropped
            logger.warning(
                "消息处理队列已满，丢弃一条消息 (key=%s, 累计丢弃 %d)", key, dropped
            )
            return False

    def stop(self, timeout: float = 5.0) -> None:
        """尽力优雅停止；队列打满导致 sentinel 放不进去时由 daemon 线程兜底。"""
        with self._state_lock:
            if not self._started:
                return
            self._started = False
        for shard in self._queues:
            try:
                shard.put_nowait((_STOP, ()))
            except queue.Full:
                pass
        per_thread = timeout / max(1, len(self._threads))
        for thread in self._threads:
            thread.join(timeout=per_thread)
        self._threads.clear()

    def _worker_loop(self, shard: queue.Queue) -> None:
        while True:
            fn, args = shard.get()
            if fn is _STOP:
                return
            try:
                fn(*args)
            except Exception:
                logger.exception("消息处理任务异常")
