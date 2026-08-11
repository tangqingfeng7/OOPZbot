"""域上下文解析与后台成员列表缓存。"""

from __future__ import annotations

import time

import web.web_player_config as cfg
from app.services.playback import PlaybackAreaResolver, PlaybackAreaUnavailable
from core.logger_config import get_logger
from web.web_link_token import get_active_area

from ._runtime import _get_redis, _get_sender
from ._ttl_cache import TtlCache

logger = get_logger("WebPlayerAdmin")

_resolved_area_cache: dict = {"value": "", "ts": 0.0}


def _resolve_area() -> str:
    """同步获取当前域 ID：优先配置，其次由异步探测写入的缓存（5 分钟）。

    本函数不做网络 I/O，可安全用于同步上下文（如 ``PlaybackAreaResolver`` 的读取回调）；
    需要在未配置默认域时真正向网关探测，请用 ``_resolve_area_async``。
    """
    area = (cfg.OOPZ_CONFIG.get("default_area") or "").strip()
    if area:
        return area
    if _resolved_area_cache["value"] and time.time() - _resolved_area_cache["ts"] < 300:
        return _resolved_area_cache["value"]
    return ""


async def _resolve_area_async() -> str:
    """获取当前域 ID；缓存未命中时从已加入的域列表取第一个并回写缓存。"""
    area = _resolve_area()
    if area:
        return area
    sender = _get_sender()
    if not sender:
        return ""
    try:
        areas = await sender.get_joined_areas(quiet=True)
        if areas:
            resolved = (areas[0].get("id") or "").strip()
            if resolved:
                _resolved_area_cache.update(value=resolved, ts=time.time())
                return resolved
    except Exception:
        logger.debug("自动解析默认域失败", exc_info=True)
    return ""


async def _music_area_context(redis_client=None) -> dict:
    """返回后台音乐当前使用的域上下文。"""
    r = redis_client or await _get_redis()
    default_area = (cfg.OOPZ_CONFIG.get("default_area") or "").strip()
    active_area = ""
    try:
        active_area = (await get_active_area(redis_client=r) or "").strip()
    except Exception:
        logger.debug("读取活跃域失败，继续尝试默认域/自动探测", exc_info=True)

    resolution = PlaybackAreaResolver(
        active_area_reader=lambda: active_area,
        default_area_reader=lambda: default_area,
        joined_area_reader=_resolve_area,
    ).admin()
    area = resolution.value
    source = resolution.source

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


async def _get_music_area(redis_client=None) -> str:
    """获取音乐相关接口应使用的域。"""
    return (await _music_area_context(redis_client)).get("area", "")


async def _require_music_area(redis_client=None) -> str:
    area = await _get_music_area(redis_client)
    if not area:
        raise PlaybackAreaUnavailable(PlaybackAreaUnavailable.message)
    return area


def _playback_area_unavailable_payload() -> dict:
    return {
        "ok": False,
        "code": PlaybackAreaUnavailable.code,
        "error": PlaybackAreaUnavailable.message,
    }


_MEMBERS_RESP_TTL = 10.0  # 管理后台成员列表响应缓存 10 秒
# 按 "域:offset:limit" 分槽：单槽缓存会在切域时互相挤掉，来回切就永远不命中。
_members_resp_cache = TtlCache(_MEMBERS_RESP_TTL)


def _invalidate_members_cache() -> None:
    """管理操作后清除成员列表缓存,让下次请求拿到最新数据。"""
    _members_resp_cache.invalidate()
    sender = _get_sender()
    if sender:
        store = getattr(sender, "_area_members_cache", None)
        if isinstance(store, dict):
            store.clear()


__all__ = [
    "_MEMBERS_RESP_TTL",
    "_get_music_area",
    "_invalidate_members_cache",
    "_members_resp_cache",
    "_music_area_context",
    "_playback_area_unavailable_payload",
    "_require_music_area",
    "_resolve_area",
    "_resolve_area_async",
    "_resolved_area_cache",
]
