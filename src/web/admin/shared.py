"""管理后台共享依赖和辅助函数。"""
# pyright: reportMissingModuleSource=false

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import secrets
import string
import sys
import time
from collections import deque
from http.cookies import SimpleCookie
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

try:
    import requests

    RequestsException = requests.RequestException
except Exception:
    requests = None
    RequestsException = RuntimeError

try:
    import qrcode  # type: ignore[reportMissingModuleSource]
except Exception:
    qrcode = None

from core.database import DB_PATH, MessageStatsDB, ReminderDB, ScheduledMessageDB, SongCache, Statistics, db_connection
from core.logger_config import get_logger
from oopz.name_resolver import get_resolver
from core.queue_manager import get_redis_client, _area_key, KEY_QUEUE, KEY_CURRENT, KEY_PLAY_STATE
from services.scheduler_templates import get_scheduled_template, list_scheduled_templates
from web.web_link_token import clear_token, ensure_token, get_active_area, get_token, set_token

import web.web_player_config as cfg
from app.services.interaction.setup_diagnostics import SetupDiagnostics

logger = get_logger("WebPlayerAdmin")

_oopz_login_lock = asyncio.Lock()
_OOPZ_RUNTIME_FIELDS = ("app_version", "device_id", "person_uid", "jwt_token")


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _get_redis():
    """延迟导入，避免循环引用。"""
    from web.web_player import get_redis
    return get_redis()


def _get_sender():
    from web.web_player import get_sender
    return get_sender()


def _get_oopz_client():
    from web.web_player import get_oopz_client
    return get_oopz_client()


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


def _get_netease():
    from web.web_player import get_netease
    return get_netease()


def _get_started_at() -> float:
    from web.web_player import started_at
    return started_at


def _admin_enabled() -> bool:
    from web.web_player import _admin_enabled as web_admin_enabled
    return web_admin_enabled()


def _get_liked_ids_cache() -> list:
    from web.web_player import liked_ids_cache
    return liked_ids_cache


def _get_plugin_runtime():
    from web.web_player import get_plugin_runtime
    return get_plugin_runtime()


def _set_liked_ids_cache(value: list) -> None:
    import web.web_player as web_player
    web_player.liked_ids_cache = value


def _normalize_netease_base_url(raw: object = "") -> str:
    """校验并规范化后台传入的网易云 API 地址。"""
    value = str(raw or cfg.NETEASE_CLOUD.get("base_url") or "").strip().rstrip("/")
    if not value:
        raise ValueError("网易云 API 地址为空")
    if len(value) > 300:
        raise ValueError("网易云 API 地址过长")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("网易云 API 地址必须是 http/https URL")
    return value


def _netease_timestamp_params(extra: dict | None = None) -> dict:
    """附加时间戳参数，避免网易云登录接口被缓存。"""
    stamp = str(int(time.time() * 1000))
    params = {
        "timestamp": stamp,
        "timerstamp": stamp,
    }
    if extra:
        params.update(extra)
    return params


def _netease_api_get(
    base_url: str,
    path: str,
    params: dict | None = None,
    headers: dict | None = None,
) -> tuple[dict, Any]:
    """请求本地/远端 NeteaseCloudMusicApi 并返回 JSON。"""
    if requests is None:
        raise RuntimeError("缺少 requests 依赖，请先安装 requirements.txt")
    request_headers = {"Cache-Control": "no-cache"}
    if headers:
        request_headers.update(headers)
    response = requests.get(
        f"{base_url}{path}",
        params=params or {},
        headers=request_headers,
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("网易云 API 返回格式异常")
    return data, response


def _netease_api_post(
    base_url: str,
    path: str,
    data: dict | None = None,
    headers: dict | None = None,
) -> tuple[dict, Any]:
    """POST 请求网易云 API；用于避免把长 Cookie 暴露在查询串里。"""
    if requests is None:
        raise RuntimeError("缺少 requests 依赖，请先安装 requirements.txt")
    request_headers = {"Cache-Control": "no-cache"}
    if headers:
        request_headers.update(headers)
    response = requests.post(
        f"{base_url}{path}",
        data=data or {},
        headers=request_headers,
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("网易云 API 返回格式异常")
    return payload, response


def _netease_response_data(payload: dict) -> dict:
    """兼容不同网易云 API 分支的 data 包装结构。"""
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _netease_qr_code(payload: dict) -> int:
    """从扫码检查响应中提取状态码。"""
    status_codes = {800, 801, 802, 803}
    values = (_netease_response_data(payload).get("code"), payload.get("code"))
    parsed_values = []
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed in status_codes:
            return parsed
        parsed_values.append(parsed)
    if parsed_values:
        return parsed_values[0]
    return 0


def _cookie_from_response(payload: dict, response: Any) -> str:
    """优先取接口 JSON 中的 Cookie，回退到响应 Set-Cookie。"""
    nested = _netease_response_data(payload)
    cookie = str(payload.get("cookie") or nested.get("cookie") or "").strip()
    if cookie:
        logger.debug("网易云扫码登录 Cookie 提取成功: source=json %s", _cookie_debug_summary(cookie))
        return cookie

    jar = getattr(response, "cookies", None)
    if jar:
        try:
            pairs = [f"{item.name}={item.value}" for item in jar]
            cookie = "; ".join(pair for pair in pairs if pair)
            if cookie:
                logger.debug("网易云扫码登录 Cookie 提取成功: source=response %s", _cookie_debug_summary(cookie))
                return cookie
        except Exception:
            logger.debug("解析网易云登录响应 CookieJar 失败", exc_info=True)

    header = response.headers.get("set-cookie", "") if hasattr(response, "headers") else ""
    cookie = header.strip()
    if cookie:
        logger.debug("网易云扫码登录 Cookie 提取成功: source=set-cookie %s", _cookie_debug_summary(cookie))
    else:
        logger.debug("网易云扫码登录响应中未找到可用 Cookie")
    return cookie


def _netease_login_message(payload: dict, default: str = "") -> str:
    """兼容 message/msg/nested message 三种提示字段。"""
    nested = _netease_response_data(payload)
    return str(
        payload.get("message")
        or payload.get("msg")
        or nested.get("message")
        or nested.get("msg")
        or default
    )


def _extract_netease_profile(payload: dict) -> Optional[dict]:
    """从账号接口返回中提取昵称和用户 ID。"""
    nested = _netease_response_data(payload)
    profile = payload.get("profile") or nested.get("profile") or {}
    account = payload.get("account") or nested.get("account") or {}
    if not isinstance(profile, dict):
        profile = {}
    if not isinstance(account, dict):
        account = {}

    user_id = (
        profile.get("userId")
        or profile.get("userid")
        or profile.get("id")
        or account.get("id")
        or account.get("userId")
    )
    if not user_id:
        return None

    nickname = (
        profile.get("nickname")
        or profile.get("name")
        or account.get("userName")
        or account.get("nickname")
        or ""
    )
    return {
        "user_id": str(user_id),
        "nickname": str(nickname or ""),
        "avatar_url": str(profile.get("avatarUrl") or profile.get("avatarUrlHttps") or ""),
    }


def _netease_account_status(base_url: str, cookie: str) -> dict:
    """使用 Cookie 查询当前网易云登录账号。"""
    cookie = (cookie or "").strip()
    if not cookie:
        logger.debug("网易云账号状态查询跳过: 未配置 Cookie")
        return {"ok": True, "logged_in": False, "message": "未配置网易云 Cookie"}

    logger.debug("网易云账号状态查询开始: base_url=%s %s", base_url, _cookie_debug_summary(cookie))
    requests_to_try = (
        (
            "POST",
            "/login/status",
            _netease_timestamp_params({"cookie": cookie}),
        ),
        (
            "GET",
            "/user/account",
            _netease_timestamp_params(),
        ),
    )
    last_message = ""
    for method, path, params in requests_to_try:
        try:
            logger.debug("网易云账号状态请求: method=%s path=%s", method, path)
            if method == "POST":
                payload, _ = _netease_api_post(base_url, path, data=params, headers={"Cookie": cookie})
            else:
                payload, _ = _netease_api_get(base_url, path, params=params, headers={"Cookie": cookie})
        except Exception as exc:
            last_message = str(exc)
            logger.debug("网易云账号状态请求失败 (%s %s): %s", method, path, exc)
            continue

        nested = _netease_response_data(payload)
        logger.debug(
            "网易云账号状态接口返回: path=%s code=%s data_code=%s message=%s",
            path,
            payload.get("code"),
            nested.get("code") if isinstance(nested, dict) else None,
            _netease_login_message(payload, ""),
        )
        profile = _extract_netease_profile(payload)
        if profile:
            logger.debug("网易云账号状态查询成功: path=%s %s", path, _debug_profile_text(profile))
            return {
                "ok": True,
                "logged_in": True,
                "profile": profile,
                "message": "网易云账号已登录",
            }
        last_message = _netease_login_message(payload, last_message)
        logger.debug("网易云账号状态未解析到 profile: path=%s message=%s", path, last_message)

    logger.debug("网易云账号状态查询未登录: message=%s", last_message)
    return {
        "ok": True,
        "logged_in": False,
        "message": last_message or "Cookie 未登录或已过期",
    }


_BILIBILI_LOGIN_BASE = "https://passport.bilibili.com"
_BILIBILI_API_BASE = "https://api.bilibili.com"
_BILIBILI_QR_GENERATE_PATH = "/x/passport-login/web/qrcode/generate"
_BILIBILI_QR_POLL_PATH = "/x/passport-login/web/qrcode/poll"
_BILIBILI_NAV_PATH = "/x/web-interface/nav"
_BILIBILI_COOKIE_NAMES = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
)


def _make_qr_data_uri(content: str) -> str:
    """把登录 URL 渲染为前端可直接展示的二维码 PNG。"""
    if qrcode is None:
        raise RuntimeError("缺少 qrcode 依赖，请先安装 requirements.txt")
    image = qrcode.make(content)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _bilibili_api_get(path: str, params: dict | None = None) -> tuple[dict, Any]:
    """请求 B 站 Web 扫码登录接口并返回 JSON。"""
    if requests is None:
        raise RuntimeError("缺少 requests 依赖，请先安装 requirements.txt")
    response = requests.get(
        f"{_BILIBILI_LOGIN_BASE}{path}",
        params=params or {},
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
            "Cache-Control": "no-cache",
        },
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("B 站 API 返回格式异常")
    return data, response


def _bilibili_account_api_get(path: str, headers: dict | None = None) -> tuple[dict, Any]:
    """请求 B 站 Web API，用于读取已登录账号信息。"""
    if requests is None:
        raise RuntimeError("缺少 requests 依赖，请先安装 requirements.txt")
    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
        "Cache-Control": "no-cache",
    }
    if headers:
        request_headers.update(headers)
    response = requests.get(
        f"{_BILIBILI_API_BASE}{path}",
        headers=request_headers,
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("B 站 API 返回格式异常")
    return data, response


def _bilibili_response_data(payload: dict) -> dict:
    """兼容 B 站响应中的 data 包装结构。"""
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _bilibili_login_message(payload: dict, default: str = "") -> str:
    """提取 B 站扫码接口返回的提示。"""
    nested = _bilibili_response_data(payload)
    return str(
        nested.get("message")
        or payload.get("message")
        or payload.get("msg")
        or default
    )


def _bilibili_qr_code(payload: dict) -> int:
    """从 B 站扫码轮询响应中提取状态码。"""
    nested = _bilibili_response_data(payload)
    for value in (nested.get("code"), payload.get("code")):
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return -1


def _cookie_pairs_from_response(response: Any, allowed_names: tuple[str, ...] = ()) -> str:
    """从 CookieJar 或 Set-Cookie 头中整理出可配置的 Cookie 字符串。"""
    allowed = set(allowed_names)
    pairs: list[str] = []
    jar = getattr(response, "cookies", None)
    if jar:
        try:
            for item in jar:
                name = getattr(item, "name", "")
                value = getattr(item, "value", "")
                if name and value and (not allowed or name in allowed):
                    pairs.append(f"{name}={value}")
        except Exception:
            logger.debug("解析登录响应 CookieJar 失败", exc_info=True)
    if pairs:
        return "; ".join(pairs)

    header = response.headers.get("set-cookie", "") if hasattr(response, "headers") else ""
    if header:
        try:
            cookie = SimpleCookie()
            cookie.load(header)
            for name, morsel in cookie.items():
                if morsel.value and (not allowed or name in allowed):
                    pairs.append(f"{name}={morsel.value}")
        except Exception:
            logger.debug("解析 Set-Cookie 头失败", exc_info=True)
    return "; ".join(pairs)


def _mask_debug_token(value: str, prefix: int = 6, suffix: int = 4) -> str:
    """日志中遮罩 token/key，避免泄露可复用凭据。"""
    text = str(value or "")
    if not text:
        return "-"
    if len(text) <= prefix + suffix:
        return "*" * len(text)
    return f"{text[:prefix]}...{text[-suffix:]}"


def _cookie_debug_summary(cookie: str) -> str:
    """生成不含 Cookie 值的调试摘要。"""
    text = (cookie or "").strip()
    names: list[str] = []
    if text:
        try:
            parsed = SimpleCookie()
            parsed.load(text)
            names = [name for name, morsel in parsed.items() if morsel.value]
        except Exception:
            names = []
        if not names:
            for item in text.split(";"):
                name = item.split("=", 1)[0].strip()
                if name:
                    names.append(name)
    names_text = ",".join(names) if names else "-"
    return f"len={len(text)} names={names_text}"


def _debug_profile_text(profile: Optional[dict]) -> str:
    """生成账号资料调试文本。"""
    if not profile:
        return "profile=-"
    return "nickname=%s uid=%s" % (
        profile.get("nickname") or "",
        profile.get("user_id") or "",
    )


def _bilibili_cookie_from_poll(payload: dict, response: Any) -> str:
    """优先取 Set-Cookie，回退到跨域登录 URL 中的 Cookie 参数。"""
    cookie = _cookie_pairs_from_response(response, _BILIBILI_COOKIE_NAMES)
    if cookie:
        logger.debug("B 站扫码登录 Cookie 提取成功: source=response %s", _cookie_debug_summary(cookie))
        return cookie

    login_url = str(_bilibili_response_data(payload).get("url") or "").strip()
    if not login_url:
        logger.debug("B 站扫码登录未拿到跨域登录 URL，无法回退提取 Cookie")
        return ""
    try:
        query = parse_qs(urlparse(login_url).query)
    except Exception:
        logger.debug("B 站扫码登录 URL 解析失败，无法回退提取 Cookie", exc_info=True)
        return ""
    pairs = []
    for name in _BILIBILI_COOKIE_NAMES:
        values = query.get(name)
        if values and values[0]:
            pairs.append(f"{name}={values[0]}")
    cookie = "; ".join(pairs)
    if cookie:
        logger.debug("B 站扫码登录 Cookie 提取成功: source=url %s", _cookie_debug_summary(cookie))
    else:
        logger.debug("B 站扫码登录 URL 中未找到可用 Cookie 字段")
    return cookie


def _extract_bilibili_profile(payload: dict) -> Optional[dict]:
    """从 B 站导航栏接口返回中提取昵称和 UID。"""
    data = _bilibili_response_data(payload)
    if not isinstance(data, dict) or not data.get("isLogin"):
        return None
    user_id = data.get("mid") or data.get("uid")
    if not user_id:
        return None
    return {
        "user_id": str(user_id),
        "nickname": str(data.get("uname") or data.get("name") or ""),
        "avatar_url": str(data.get("face") or ""),
    }


def _bilibili_account_status(cookie: str) -> dict:
    """使用 Cookie 查询当前 B 站登录账号。"""
    cookie = (cookie or "").strip()
    if not cookie:
        logger.debug("B 站账号状态查询跳过: 未配置 Cookie")
        return {"ok": True, "logged_in": False, "message": "未配置 B 站 Cookie"}

    logger.debug("B 站账号状态查询开始: %s", _cookie_debug_summary(cookie))
    payload, _ = _bilibili_account_api_get(
        _BILIBILI_NAV_PATH,
        headers={"Cookie": cookie},
    )
    data = _bilibili_response_data(payload)
    logger.debug(
        "B 站账号状态接口返回: code=%s isLogin=%s message=%s",
        payload.get("code"),
        data.get("isLogin") if isinstance(data, dict) else None,
        _bilibili_login_message(payload, ""),
    )
    profile = _extract_bilibili_profile(payload)
    if profile:
        logger.debug("B 站账号状态查询成功: %s", _debug_profile_text(profile))
        return {
            "ok": True,
            "logged_in": True,
            "profile": profile,
            "message": "B 站账号已登录",
        }

    message = _bilibili_login_message(payload, "Cookie 未登录或已过期")
    logger.debug("B 站账号状态查询未登录: message=%s", message)
    return {"ok": True, "logged_in": False, "message": message}


_ADMIN_SHELL_TEMPLATE: string.Template | None = None


def _load_admin_template() -> string.Template:
    global _ADMIN_SHELL_TEMPLATE
    if _ADMIN_SHELL_TEMPLATE is None:
        assets_root = os.path.dirname(os.path.dirname(__file__))
        tpl_path = os.path.join(assets_root, "assets", "admin", "admin-shell-template.html")
        with open(tpl_path, "r", encoding="utf-8") as f:
            _ADMIN_SHELL_TEMPLATE = string.Template(f.read())
    return _ADMIN_SHELL_TEMPLATE


_ADMIN_PAGES: dict[str, dict[str, Any]] = {
    "dashboard": {
        "page_title": "后台总览",
        "page_id": "dashboard",
        "brand_title": "后台管理",
        "brand_copy": "顶部主导航、数据优先、专业 SaaS 工作台。",
        "topbar_actions": [
            {"action": "refresh-overview", "label": "刷新概览"},
        ],
        "login_title": "登录后台总览",
        "login_copy": "登录后查看实时状态与关键指标。",
        "login_button": "进入总览",
    },
    "music": {
        "page_title": "音乐管理",
        "page_id": "music",
        "brand_title": "音乐管理",
        "brand_copy": "把播放控制、搜索加歌和队列调度整理成标准运营面板。",
        "topbar_actions": [
            {"action": "refresh-queue", "label": "刷新队列"},
        ],
        "login_title": "登录音乐后台",
        "login_copy": "登录后控制播放、搜索歌曲和调整队列。",
        "login_button": "进入音乐控制台",
    },
    "config": {
        "page_title": "配置中心",
        "page_id": "config",
        "brand_title": "配置中心",
        "brand_copy": "把长表单整理成章节化配置工作台，保留原字段和保存接口。",
        "topbar_actions": [
            {"action": "refresh-config", "label": "刷新配置"},
            {"action": "reset-overrides", "label": "恢复运行配置"},
            {"action": "save-config", "label": "保存并立即生效", "variant": "primary"},
        ],
        "login_title": "登录配置中心",
        "login_copy": "登录后调整后台配置。",
        "login_button": "进入配置中心",
    },
    "stats": {
        "page_title": "统计页",
        "page_id": "stats",
        "brand_title": "统计页",
        "brand_copy": "让摘要、榜单和危险操作形成稳定阅读顺序，而不是只摆一张表。",
        "topbar_actions": [
            {"action": "refresh-stats", "label": "刷新统计"},
            {"action": "clear-history", "label": "清空历史", "variant": "danger"},
        ],
        "login_title": "登录统计页",
        "login_copy": "登录后查看最近 7 天的播放排行。",
        "login_button": "进入统计页",
    },
    "system": {
        "page_title": "系统页",
        "page_id": "system",
        "brand_title": "系统页",
        "brand_copy": "把播放器入口、系统快照和实时日志拆成明确的运维层级。",
        "topbar_actions": [
            {"action": "refresh-system", "label": "刷新系统信息"},
            {"action": "refresh-logs", "label": "刷新日志"},
        ],
        "login_title": "登录系统页",
        "login_copy": "登录后查看链接、系统信息和日志。",
        "login_button": "进入系统页",
    },
    "activity": {
        "page_title": "活跃统计",
        "page_id": "activity",
        "brand_title": "活跃统计",
        "brand_copy": "频道消息趋势与用户活跃排行一览。",
        "topbar_actions": [
            {"action": "refresh-activity", "label": "刷新统计"},
        ],
        "login_title": "登录活跃统计",
        "login_copy": "登录后查看消息趋势与活跃排行。",
        "login_button": "进入活跃统计",
    },
    "scheduler": {
        "page_title": "定时任务",
        "page_id": "scheduler",
        "brand_title": "定时任务",
        "brand_copy": "管理定时消息与用户提醒。",
        "topbar_actions": [
            {"action": "refresh-scheduler", "label": "刷新列表"},
        ],
        "login_title": "登录定时任务",
        "login_copy": "登录后管理定时消息与查看提醒。",
        "login_button": "进入定时任务",
    },
    "members": {
        "page_title": "成员管理",
        "page_id": "members",
        "brand_title": "成员管理",
        "brand_copy": "域成员浏览、管理操作与封禁列表。",
        "topbar_actions": [
            {"action": "refresh-members", "label": "刷新成员"},
        ],
        "login_title": "登录成员管理",
        "login_copy": "登录后管理域成员。",
        "login_button": "进入成员管理",
    },
    "areas": {
        "page_title": "域管理",
        "page_id": "areas",
        "brand_title": "域管理",
        "brand_copy": "域配置、频道管理与语音频道监控。",
        "topbar_actions": [
            {"action": "refresh-areas", "label": "刷新"},
        ],
        "login_title": "登录域管理",
        "login_copy": "登录后管理域配置与频道。",
        "login_button": "进入域管理",
    },
    "plugins": {
        "page_title": "插件管理",
        "page_id": "plugins",
        "brand_title": "插件管理",
        "brand_copy": "查看、加载、卸载插件，在线编辑插件配置。",
        "topbar_actions": [
            {"action": "refresh-plugins", "label": "刷新列表"},
        ],
        "login_title": "登录插件管理",
        "login_copy": "登录后管理插件与配置。",
        "login_button": "进入插件管理",
    },
    "setup": {
        "page_title": "系统体检",
        "page_id": "setup",
        "brand_title": "系统体检",
        "brand_copy": "把首启检查、运行时诊断和下一步配置建议放在一个页面里。",
        "topbar_actions": [
            {"action": "refresh-diagnostics", "label": "重新体检"},
        ],
        "login_title": "登录系统体检",
        "login_copy": "登录后查看系统体检与首启向导。",
        "login_button": "进入体检页",
    },
}


def _render_topbar_actions(actions: list[dict[str, str]]) -> str:
    """把结构化按钮声明渲染成顶栏 HTML，行为通过 data-action 委托到页面脚本。"""
    buttons = [
        '<button class="btn btn-{variant}" type="button" data-action="{action}">{label}</button>'.format(
            variant=action.get("variant", "ghost"),
            action=action["action"],
            label=action["label"],
        )
        for action in actions
    ]
    return "\n          ".join(buttons)


def _render_admin_page(page_key: str) -> HTMLResponse:
    if not _admin_enabled():
        return HTMLResponse("管理后台未启用，请在 WEB_PLAYER_CONFIG 中开启。", status_code=404)
    assets_root = os.path.dirname(os.path.dirname(__file__))
    pages_dir = os.path.join(assets_root, "assets", "admin", "pages")
    content_path = os.path.join(pages_dir, f"{page_key}_content.html")
    script_path = os.path.join(pages_dir, f"{page_key}_script.js")
    with open(content_path, "r", encoding="utf-8") as f:
        page_content = f.read()
    with open(script_path, "r", encoding="utf-8") as f:
        page_script = f.read()
    meta = _ADMIN_PAGES[page_key]
    tpl = _load_admin_template()
    html = tpl.safe_substitute(
        page_title=meta["page_title"],
        page_id=meta["page_id"],
        brand_title=meta["brand_title"],
        brand_copy=meta["brand_copy"],
        topbar_actions=_render_topbar_actions(meta["topbar_actions"]),
        login_title=meta["login_title"],
        login_copy=meta["login_copy"],
        login_button=meta["login_button"],
        page_content=page_content,
        page_script=page_script,
    )
    return HTMLResponse(html)


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


def _overview_payload() -> dict:
    redis_status = "connected"
    queue_len = 0
    playing: dict = {}
    area_context = _music_area_context()
    try:
        r = _get_redis()
        r.ping()
        area_context = _music_area_context(r)
        area = area_context.get("area", "")
        queue_len = int(r.llen(_area_key(KEY_QUEUE, area)) or 0)
        current_raw = r.get(_area_key(KEY_CURRENT, area))
        play_state_raw = r.get(_area_key(KEY_PLAY_STATE, area))
        playing = {
            "current": json.loads(current_raw) if current_raw else None,
            "play_state": json.loads(play_state_raw) if play_state_raw else None,
            "area": area,
        }
    except Exception as e:
        redis_status = f"error: {e}"

    today = Statistics.get_today() or {}
    summary = Statistics.get_summary()
    return {
        "ok": True,
        "uptime_seconds": int(time.time() - _get_started_at()),
        "redis": redis_status,
        "queue_length": queue_len,
        "playing": playing,
        "music_area": area_context,
        "statistics_today": today,
        "statistics_summary": summary,
        "today_messages": MessageStatsDB.get_today_total(),
        "active_users_today": MessageStatsDB.get_active_users_today(),
    }


def _tail_file(path: str, lines: int = 200) -> list[str]:
    if not os.path.exists(path):
        return []
    max_lines = max(1, min(int(lines), 2000))
    dq: deque[str] = deque(maxlen=max_lines)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            dq.append(line.rstrip("\n"))
    return list(dq)


def _top_songs_from_play_history(page: int = 1, page_size: int = 10) -> tuple[list[dict], int]:
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 10), 100))
    offset = (page - 1) * page_size
    with db_connection() as conn:
        total_row = conn.execute(
            """
            SELECT COUNT(1) AS c
            FROM (
                SELECT sc.song_id
                FROM play_history ph
                LEFT JOIN song_cache sc ON sc.id = ph.song_cache_id
                GROUP BY sc.song_id, sc.song_name, sc.artist, sc.album
            ) t
            """
        ).fetchone()
        total = int(total_row["c"] if total_row else 0)
        rows = conn.execute(
            """
            SELECT
                sc.song_id AS song_id,
                COALESCE(sc.song_name, '') AS song_name,
                COALESCE(sc.artist, '') AS artist,
                COALESCE(sc.album, '') AS album,
                COUNT(ph.id) AS play_count,
                MAX(ph.played_at) AS last_played_at
            FROM play_history ph
            LEFT JOIN song_cache sc ON sc.id = ph.song_cache_id
            GROUP BY sc.song_id, sc.song_name, sc.artist, sc.album
            ORDER BY play_count DESC, last_played_at DESC
            LIMIT ? OFFSET ?
            """,
            (page_size, offset),
        ).fetchall()
    return [dict(r) for r in rows], total


def _queue_snapshot(redis_client, area: str = "") -> list[dict]:
    items = redis_client.lrange(_area_key(KEY_QUEUE, area), 0, -1)
    queue: list[dict] = []
    for i, item in enumerate(items):
        try:
            song = json.loads(item)
        except Exception as e:
            logger.debug("解析队列项 %d 失败: %s", i, e)
            song = {}
        queue.append({
            "index": i,
            "id": song.get("song_id") or song.get("id"),
            "name": song.get("name", ""),
            "artists": song.get("artists", ""),
            "album": song.get("album", ""),
            "durationText": song.get("durationText") or song.get("duration", ""),
        })
    return queue


def _current_song_snapshot(redis_client, area: str = "") -> Optional[dict]:
    try:
        raw = redis_client.get(_area_key(KEY_CURRENT, area))
        if not raw:
            return None
        song = json.loads(raw)
        return {
            "id": song.get("song_id") or song.get("id"),
            "name": song.get("name", ""),
            "artists": song.get("artists", ""),
            "album": song.get("album", ""),
            "durationText": song.get("durationText") or song.get("duration", ""),
        }
    except Exception:
        logger.debug("读取当前播放信息失败", exc_info=True)
        return None


def _execute_control_action(action: str, body: dict, redis_client, area: str = "") -> dict:
    from web.web_player import execute_control_action
    return execute_control_action(action, body, redis_client, area=area)


def _execute_queue_action(action: str, index, redis_client, area: str = "") -> dict:
    from web.web_player import execute_queue_action
    return execute_queue_action(action, index, redis_client, area=area)


def _add_song_to_queue(body: dict, area: str = "") -> dict:
    from web.web_player import add_song_to_queue
    return add_song_to_queue(body, area=area)

__all__ = [name for name in globals() if not name.startswith("__")]
