from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from core.logger_config import get_logger
from onebot_v11.adapter import OneBotV11Adapter
from onebot_v11.config import OneBotV11ServerConfig
from onebot_v11.server import OneBotV11Server

logger = get_logger("OneBotV11Service")


class OneBotV11Service:
    """Runs the OneBot v11 aiohttp server in a background event loop."""

    def __init__(self, sender: Any, config: OneBotV11ServerConfig) -> None:
        self.config = config
        self.adapter = OneBotV11Adapter(
            sender,
            db_path=config.db_path,
            member_list_max=config.member_list_max,
            enable_area_scoped_group_ban=config.enable_area_scoped_group_ban,
            enable_set_group_kick_as_area_kick=config.enable_set_group_kick_as_area_kick,
            enable_set_group_leave_as_area_leave=config.enable_set_group_leave_as_area_leave,
            enable_set_group_admin_as_area_role=config.enable_set_group_admin_as_area_role,
            group_admin_role_id=config.group_admin_role_id,
        )
        self.server = OneBotV11Server(self.adapter, config)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._serving = threading.Event()
        self._stop_requested = threading.Event()
        self._state_lock = threading.Lock()

    def start(self) -> None:
        if not self.config.enabled:
            return
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_requested.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="OneBotV11",
                daemon=True,
            )
            self._thread.start()
        self._started.wait(timeout=5)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_requested.set()
        deadline = time.monotonic() + max(0.0, timeout)
        with self._state_lock:
            loop = self._loop
            thread = self._thread

        # 初始化阶段不打断 server.start()；启动协程完成后会看到
        # _stop_requested 并直接进入 finally 清理。已进入 run_forever 时，
        # 只需唤醒事件循环，资源统一由所属线程释放。
        if loop and self._serving.is_set() and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                logger.debug("OneBot v11 事件循环已关闭")
        current = threading.current_thread()
        if thread and thread is not current and thread.is_alive():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if thread and thread is not current and thread.is_alive():
            logger.warning("服务停止超时: OneBotV11，线程仍未退出")
        else:
            with self._state_lock:
                if self._thread is thread:
                    self._thread = None
                if self._loop is loop:
                    self._loop = None
            self._started.clear()

    def emit_raw_event(self, raw: dict[str, Any]) -> None:
        if not self.config.enabled or self._stop_requested.is_set() or not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self.adapter.emit_raw_event(raw), self._loop)

    def emit_member_change(self, action: str, area: str, uid: str) -> None:
        """Thread-safe entry for the member-list poller to report join/leave."""
        if not self.config.enabled or self._stop_requested.is_set() or not self._loop:
            return
        asyncio.run_coroutine_threadsafe(
            self.adapter.emit_member_change(action, area, uid), self._loop
        )

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        with self._state_lock:
            self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            if self._stop_requested.is_set():
                return
            loop.run_until_complete(self.server.start())
            self._started.set()
            self._serving.set()
            if self._stop_requested.is_set():
                loop.call_soon(loop.stop)
            loop.run_forever()
        except Exception:
            logger.exception("OneBot v11 服务线程异常")
        finally:
            self._serving.clear()
            self._started.clear()
            try:
                if self.server:
                    loop.run_until_complete(self.server.stop())
            finally:
                loop.close()
                with self._state_lock:
                    if self._loop is loop:
                        self._loop = None
