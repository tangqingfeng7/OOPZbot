"""域上下文解析与后台成员列表缓存。"""

from __future__ import annotations

import time

from core.logger_config import get_logger

import web.web_player_config as cfg
from web.web_link_token import get_active_area

from ._runtime import _get_redis, _get_sender

logger = get_logger("WebPlayerAdmin")

_resolved_area_cache: dict = {"value": "", "ts": 0.0}


def _resolve_area() -> str:
    """获取当前域 ID,优先使用配置,否则从已加入的域列表取第一个(缓存 5 分钟)。"""
    area = (cfg.OOPZ_CONFIG.get("default_area") or "").strip()
    if area:
        return area
    now = time.time()
    if _resolved_area_cache["value"] and now - _resolved_area_cache["ts"] < 300:
        return _resolved_area_cache["value"]
    sender = _get_sender()
    if not sender:
        return ""
    try:
        areas = sender.get_joined_areas(quiet=True)
        if areas:
            resolved = (areas[0].get("id") or "").strip()
            if resolved:
                _resolved_area_cache.update(value=resolved, ts=now)
                return resolved
    except Exception:
        logger.debug("自动解析默认域失败", exc_info=True)
    return ""


def _music_area_context(redis_client=None) -> dict:
    """返回后台音乐当前使用的域上下文。"""
    r = redis_client or _get_redis()
    default_area = (cfg.OOPZ_CONFIG.get("default_area") or "").strip()
    active_area = ""
    try:
        active_area = (get_active_area(redis_client=r) or "").strip()
    except Exception:
        logger.debug("读取活跃域失败，继续尝试默认域/自动探测", exc_info=True)

    area = ""
    source = "none"
    if active_area:
        area = active_area
        source = "active"
    elif default_area:
        area = default_area
        source = "default"
    else:
        area = _resolve_area()
        if area:
            source = "auto"

    source_text = {
        "active": "活跃域",
        "default": "默认域",
        "auto": "自动探测",
        "none": "未解析",
    }.get(source, "未解析")

    return {
        "area": area,
        "source": source,
        "source_text": source_text,
        "default_area": default_area,
        "active_area": active_area,
    }


def _get_music_area(redis_client=None) -> str:
    """获取音乐相关接口应使用的域。"""
    return _music_area_context(redis_client).get("area", "")


_members_resp_cache: dict = {"data": None, "ts": 0.0, "key": ""}
_MEMBERS_RESP_TTL = 10.0  # 管理后台成员列表响应缓存 10 秒


def _invalidate_members_cache() -> None:
    """管理操作后清除成员列表缓存,让下次请求拿到最新数据。"""
    _members_resp_cache.update(data=None, ts=0.0, key="")
    sender = _get_sender()
    if sender:
        store = getattr(sender, "_area_members_cache", None)
        if isinstance(store, dict):
            store.clear()


__all__ = [
    "_resolved_area_cache",
    "_resolve_area",
    "_music_area_context",
    "_get_music_area",
    "_members_resp_cache",
    "_MEMBERS_RESP_TTL",
    "_invalidate_members_cache",
]
