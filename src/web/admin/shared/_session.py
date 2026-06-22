"""管理后台会话令牌的写入与清除。"""

from __future__ import annotations

from core.logger_config import get_logger

import web.web_player_config as cfg

from ._runtime import _get_redis

logger = get_logger("WebPlayerAdmin")


def _set_admin_session_token(token: str) -> None:
    ttl = cfg.admin_session_ttl_seconds()
    r = _get_redis()
    if ttl > 0:
        r.set(cfg.admin_session_key(token), "1", ex=ttl)
    else:
        r.set(cfg.admin_session_key(token), "1")


def _clear_admin_session_token(token: str) -> None:
    if not token:
        return
    try:
        _get_redis().delete(cfg.admin_session_key(token))
    except Exception:
        logger.debug("清除管理后台会话令牌失败", exc_info=True)


__all__ = [
    "_set_admin_session_token",
    "_clear_admin_session_token",
]
