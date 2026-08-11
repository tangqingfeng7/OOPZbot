"""管理后台会话令牌的写入与清除。"""

from __future__ import annotations

import web.web_player_config as cfg
from core.logger_config import get_logger

from ._runtime import _get_redis

logger = get_logger("WebPlayerAdmin")


async def _set_admin_session_token(token: str) -> None:
    ttl = cfg.admin_session_ttl_seconds()
    r = await _get_redis()
    if ttl > 0:
        await r.set(cfg.admin_session_key(token), "1", ex=ttl)
    else:
        await r.set(cfg.admin_session_key(token), "1")


async def _clear_admin_session_token(token: str) -> None:
    if not token:
        return
    try:
        await (await _get_redis()).delete(cfg.admin_session_key(token))
    except Exception:
        logger.debug("清除管理后台会话令牌失败", exc_info=True)


__all__ = [
    "_clear_admin_session_token",
    "_set_admin_session_token",
]
