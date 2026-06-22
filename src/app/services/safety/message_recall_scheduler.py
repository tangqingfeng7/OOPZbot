import heapq
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from config import AUTO_RECALL_CONFIG
from app.services.runtime import CommandRuntimeView, sender_of
from core.logger_config import get_logger

logger = get_logger("MessageRecallScheduler")


@dataclass(order=True)
class _ScheduledRecall:
    due_at: float
    sequence: int
    message_id: str = field(compare=False)
    channel: str = field(compare=False)
    area: str = field(compare=False)
    timestamp: str = field(compare=False, default="")


class MessageRecallScheduler:
    """负责自动撤回判定和异步调度。"""

    def __init__(self, runtime: CommandRuntimeView):
        self._sender = sender_of(runtime)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._pending: list[_ScheduledRecall] = []
        self._worker: threading.Thread | None = None
        self._sequence = 0
        self._stopped = False

    @staticmethod
    def should_skip_auto_recall(command_type: str) -> Optional[bool]:
        """检查指定命令类型是否应跳过自动撤回。"""
        if AUTO_RECALL_CONFIG.get("enabled"):
            exclude = AUTO_RECALL_CONFIG.get("exclude_commands", [])
            if command_type in exclude:
                return False
        return None

    def schedule_user_message_recall(
        self,
        message_id: str,
        channel: str,
        area: str,
        timestamp: str = "",
    ) -> None:
        """在自动撤回开启时延迟撤回用户指令消息。"""
        if not message_id:
            return
        if not AUTO_RECALL_CONFIG.get("enabled"):
            return

        delay = AUTO_RECALL_CONFIG.get("delay", 30)
        if delay <= 0:
            return

        with self._lock:
            max_pending = self._max_pending()
            if len(self._pending) >= max_pending:
                logger.warning(
                    "自动撤回待处理任务已满 (%d)，跳过 message_id=%s",
                    max_pending,
                    str(message_id)[:16],
                )
                return

            self._sequence += 1
            heapq.heappush(
                self._pending,
                _ScheduledRecall(
                    due_at=time.monotonic() + float(delay),
                    sequence=self._sequence,
                    message_id=message_id,
                    channel=channel,
                    area=area,
                    timestamp=timestamp,
                ),
            )
            self._ensure_worker_locked()
            self._condition.notify()

    def cancel_all(self) -> int:
        """取消所有待执行的撤回任务，返回取消数量。"""
        with self._lock:
            count = len(self._pending)
            self._pending.clear()
            self._condition.notify()
        return count

    def stop(self) -> int:
        """停止后台调度线程，并取消所有待执行任务。"""
        with self._lock:
            count = len(self._pending)
            self._pending.clear()
            self._stopped = True
            self._condition.notify_all()
            worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=3)
        return count

    @staticmethod
    def _max_pending() -> int:
        try:
            return max(1, int(AUTO_RECALL_CONFIG.get("max_pending", 1000) or 1000))
        except (TypeError, ValueError):
            return 1000

    def _ensure_worker_locked(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stopped = False
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="MessageRecallScheduler",
            daemon=True,
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                while not self._stopped and not self._pending:
                    self._condition.wait()
                if self._stopped:
                    return

                job = self._pending[0]
                wait_seconds = job.due_at - time.monotonic()
                if wait_seconds > 0:
                    self._condition.wait(timeout=wait_seconds)
                    continue
                heapq.heappop(self._pending)

            try:
                self._sender.recall_message(
                    message_id=job.message_id,
                    area=job.area,
                    channel=job.channel,
                    timestamp=job.timestamp,
                )
            except Exception:
                logger.warning("自动撤回执行失败: message_id=%s", str(job.message_id)[:16], exc_info=True)
