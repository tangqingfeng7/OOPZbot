"""插件共享的后台间隔线程基类。

统一封装守护线程的「启动一次 / 停止 / 间隔轮询」生命周期，避免各推送线程

"""

from __future__ import annotations

import threading
from typing import Optional

from core.logger_config import get_logger


class IntervalWorker:
    """按固定间隔运行 ``_tick`` 的守护线程基类。

    线程以 ``_stop_event.wait(interval)`` 驱动：返回 False 表示到点执行一轮，
    返回 True 表示收到停止信号并退出。``_tick`` 抛出的异常会被记录而不致使线程退出。
    """

    _NAME = "IntervalWorker"

    def __init__(self, interval: int) -> None:
        self._interval = max(1, int(interval))
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._logger = get_logger(self._NAME)

    @property
    def stopping(self) -> bool:
        """供 ``_tick`` 在长循环中检查是否应提前退出。"""
        return self._stop_event.is_set()

    def _start_thread(self) -> bool:
        """启动后台线程；若已在运行返回 False（幂等）。"""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name=self._NAME, daemon=True)
            self._thread.start()
            return True

    def stop(self, join_timeout: float = 3.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=join_timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                self._tick()
            except Exception:
                self._logger.exception("%s: tick failed", self._NAME)

    def _tick(self) -> None:
        raise NotImplementedError
