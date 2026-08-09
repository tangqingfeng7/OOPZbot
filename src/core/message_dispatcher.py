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
import time
from collections.abc import Callable
from typing import Any

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
        self._stopping = False
        self._state_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._stop_signaled = [False] * self._worker_count
        self._drain_condition = threading.Condition()
        self._pending = 0
        self._dropped = 0
        self._dropped_lock = threading.Lock()

    @property
    def dropped_count(self) -> int:
        return self._dropped

    def start(self) -> None:
        with self._state_lock:
            self._start_locked()

    def _start_locked(self) -> None:
        """在持有 _state_lock 时启动 worker；停过的实例不允许重启。"""
        if self._started or self._stopping:
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
        """按 key 分片入队。返回 False 表示消息未入队（队列已满或正在关停）。"""
        # 状态检查、首次启动和实际入队必须与 stop 设置 _stopping 共用同一临界区。
        # 否则 submit 可先通过检查，stop 随后投递 sentinel 并让 worker 退出，
        # 最后 submit 才把任务放到已无人消费的队列里，却仍返回 True。
        with self._state_lock:
            if self._stopping:
                return False
            self._start_locked()
            shard = self._queues[hash(key) % self._worker_count]
            with self._drain_condition:
                self._pending += 1
            try:
                shard.put_nowait((fn, args))
                return True
            except queue.Full:
                with self._drain_condition:
                    self._pending -= 1
                    self._drain_condition.notify_all()
                with self._dropped_lock:
                    self._dropped += 1
                    dropped = self._dropped
                logger.warning(
                    "消息处理队列已满，丢弃一条消息 (key=%s, 累计丢弃 %d)",
                    key,
                    dropped,
                )
                return False

    def stop(self, timeout: float = 5.0) -> bool:
        """停止工作线程，返回 worker 是否已全部退出。

        原先 sentinel 用 ``put_nowait``，队列打满时被静默丢弃（``except
        queue.Full: pass``），工作线程收不到停止信号，积压的消息随进程退出
        一起丢掉。现在先拒收新消息（见 :meth:`submit`），等积压排干后再投
        sentinel —— 队列只会变短，drain 有终止性。

        ``timeout`` 是整个关停过程的总预算，不再按线程数均分：均分会让分片
        多的时候每个线程只剩几十毫秒，实际等同于不等。如果预算用尽，
        已经开始的任务不会被强制终止，但队列中尚未开始的任务会被取消，并在
        其后放入 sentinel，使 daemon worker 在当前任务返回后最终退出。
        """
        deadline = time.monotonic() + max(0.0, timeout)
        # 串行多个 stop 调用，避免重复取消任务或投递 sentinel。
        with self._stop_lock:
            with self._state_lock:
                # 即使尚未 start，stop 也是不可逆的终态。
                self._stopping = True
                threads = list(self._threads)

            if not threads:
                with self._state_lock:
                    self._started = False
                return True

            with self._drain_condition:
                while self._pending > 0:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._drain_condition.wait(timeout=remaining)
                pending = self._pending

            if pending:
                cancelled = self._cancel_waiting_tasks()
                with self._drain_condition:
                    in_flight = self._pending
                logger.warning(
                    "关停超时，取消 %d 条尚未开始的消息，仍有 %d 条正在处理",
                    cancelled,
                    in_flight,
                )

            # stop 已经在 _state_lock 下封住 submit，取消等待任务后
            # 队列不会再变满，因此即使预算已用尽也能无阻塞地放入 sentinel。
            for index, shard in enumerate(self._queues):
                if self._stop_signaled[index]:
                    continue
                try:
                    shard.put_nowait((_STOP, ()))
                    self._stop_signaled[index] = True
                except queue.Full:
                    # 标准 Queue 在上述不变式下不会走到这里；保留防御性
                    # 日志，下一次 stop 仍会尝试补投。
                    logger.warning("关停信号投递失败，该分片 worker 仍未停止")

            alive_threads: list[threading.Thread] = []
            for thread in threads:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    logger.warning("服务停止超时: %s，线程仍未退出", thread.name)
                    alive_threads.append(thread)
            with self._state_lock:
                self._started = bool(alive_threads)
                # _stopping 不复位：停过的分发器不再接收消息。否则进程退出途中
                # 迟到的一条消息会把工作线程重新拉起来。
            return not alive_threads

    def _cancel_waiting_tasks(self) -> int:
        """取消还在分片队列中、尚未被 worker 取走的任务。"""
        cancelled = 0
        for index, shard in enumerate(self._queues):
            # 已投递 sentinel 的分片不可再抽取，否则可能把唤醒信号移除。
            if self._stop_signaled[index]:
                continue
            while True:
                try:
                    fn, _args = shard.get_nowait()
                except queue.Empty:
                    break
                shard.task_done()
                if fn is _STOP:
                    # 防御非标准队列或外部测试替换；不吞掉停止信号。
                    shard.put_nowait((_STOP, ()))
                    self._stop_signaled[index] = True
                    break
                cancelled += 1

        if cancelled:
            with self._drain_condition:
                self._pending -= cancelled
                self._drain_condition.notify_all()
        return cancelled

    def _worker_loop(self, shard: queue.Queue) -> None:
        while True:
            fn, args = shard.get()
            if fn is _STOP:
                shard.task_done()
                return
            try:
                fn(*args)
            except Exception:
                logger.exception("消息处理任务异常")
            finally:
                shard.task_done()
                with self._drain_condition:
                    self._pending -= 1
                    self._drain_condition.notify_all()
