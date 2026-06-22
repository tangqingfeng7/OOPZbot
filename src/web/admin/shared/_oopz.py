"""OOPZ 凭据热更新：把新登录凭据同步到发送端、WebSocket 客户端与名称解析器。"""

from __future__ import annotations

import asyncio
from typing import Any

from core.logger_config import get_logger

import web.web_player_config as cfg

from ._runtime import _get_oopz_client, _get_sender

logger = get_logger("WebPlayerAdmin")

_oopz_login_lock = asyncio.Lock()
_OOPZ_RUNTIME_FIELDS = ("app_version", "device_id", "person_uid", "jwt_token")


def _oopz_runtime_updates(credentials: dict[str, Any]) -> dict[str, Any]:
    """提取可直接同步到 OOPZ_CONFIG 的字段。"""
    return {key: credentials.get(key) for key in _OOPZ_RUNTIME_FIELDS if credentials.get(key)}


def _apply_oopz_config_updates(credentials: dict[str, Any]) -> bool:
    updates = _oopz_runtime_updates(credentials)
    if not updates:
        return False
    try:
        import config as runtime_config
        runtime_config.OOPZ_CONFIG.update(updates)
        cfg.OOPZ_CONFIG.update(updates)
        return True
    except Exception:
        logger.debug("同步 OOPZ_CONFIG 到运行时失败", exc_info=True)
        return False


def _refresh_oopz_sender_private_key(pem: str) -> bool:
    if not pem:
        return False
    try:
        from oopz.oopz_password_login import load_private_key_from_pem

        sender = _get_sender()
        if not sender or not getattr(sender, "signer", None):
            return False
        sender.signer.private_key = load_private_key_from_pem(pem)
        cache = getattr(sender, "_area_members_cache", None)
        if isinstance(cache, dict):
            cache.clear()
        return True
    except Exception:
        logger.debug("刷新 OOPZ 发送端签名器失败", exc_info=True)
        return False


def _refresh_oopz_name_resolver(pem: str) -> bool:
    if not pem:
        return False
    try:
        from oopz.oopz_password_login import load_private_key_from_pem
        import oopz.name_resolver as name_resolver

        resolver = getattr(name_resolver, "_resolver", None)
        if resolver is None:
            return False
        resolver._private_key = load_private_key_from_pem(pem)
        resolver._config = cfg.OOPZ_CONFIG
        resolver._api_ready = True
        return True
    except Exception:
        logger.debug("刷新名称解析器 OOPZ 凭据失败", exc_info=True)
        return False


def _reload_private_key_module() -> None:
    try:
        import importlib
        import private_key
        importlib.reload(private_key)
    except Exception:
        logger.debug("重新加载 private_key.py 失败，继续使用内存中的新私钥", exc_info=True)


def _refresh_oopz_websocket(credentials: dict[str, Any]) -> bool:
    if not all(credentials.get(k) for k in ("person_uid", "device_id", "jwt_token")):
        return False
    client = _get_oopz_client()
    if not client:
        return False
    try:
        client.update_credentials(
            credentials["person_uid"],
            credentials["device_id"],
            credentials["jwt_token"],
            reconnect=True,
        )
        return True
    except Exception:
        logger.debug("刷新 OOPZ WebSocket 凭据失败", exc_info=True)
        return False


def _refresh_oopz_runtime(credentials: dict[str, Any]) -> dict[str, bool]:
    """将新 OOPZ 凭据同步到已创建的发送端和 WebSocket 客户端。"""
    pem = str(credentials.get("private_key_pem") or "").strip()
    if pem:
        _reload_private_key_module()

    refreshed = {
        "config": _apply_oopz_config_updates(credentials),
        "sender_signer": _refresh_oopz_sender_private_key(pem),
        "websocket_client": _refresh_oopz_websocket(credentials),
        "name_resolver": _refresh_oopz_name_resolver(pem),
    }
    return refreshed


__all__ = [
    "_oopz_login_lock",
    "_OOPZ_RUNTIME_FIELDS",
    "_oopz_runtime_updates",
    "_apply_oopz_config_updates",
    "_refresh_oopz_sender_private_key",
    "_refresh_oopz_name_resolver",
    "_reload_private_key_module",
    "_refresh_oopz_websocket",
    "_refresh_oopz_runtime",
]
