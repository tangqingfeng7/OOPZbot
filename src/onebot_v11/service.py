from __future__ import annotations

import asyncio
import threading
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
            enable_area_scoped_group_ban=config.enable_area_scoped_group_ban,
            enable_set_group_kick_as_area_kick=config.enable_set_group_kick_as_area_kick,
            enable_set_group_leave_as_area_leave=config.enable_set_group_leave_as_area_leave,
        )
        self.server = OneBotV11Server(self.adapter, config)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    def start(self) -> None:
        if not self.config.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name="OneBotV11", daemon=True)
        self._thread.start()
        self._started.wait(timeout=5)

    def stop(self) -> None:
        if not self._loop:
            return
        future = asyncio.run_coroutine_threadsafe(self.server.stop(), self._loop)
        try:
            future.result(timeout=5)
        except Exception as exc:
            logger.warning("停止 OneBot v11 服务失败: %s", exc)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._loop = None
        self._thread = None
        self._started.clear()

    def emit_raw_event(self, raw: dict[str, Any]) -> None:
        if not self.config.enabled or not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self.adapter.emit_raw_event(raw), self._loop)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.server.start())
            self._started.set()
            loop.run_forever()
        except Exception:
            logger.exception("OneBot v11 服务线程异常")
        finally:
            try:
                if self.server:
                    loop.run_until_complete(self.server.stop())
            finally:
                loop.close()
                self._started.clear()
