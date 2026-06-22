"""网易云音乐 API 辅助：扫码登录、Cookie 提取与账号状态查询。"""

from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import urlparse

from core.http_constants import HTTP_TIMEOUT_DEFAULT
from core.logger_config import get_logger

import web.web_player_config as cfg

from ._debug import _cookie_debug_summary, _cookie_pairs_from_header, _debug_profile_text

try:
    import requests

    RequestsException = requests.RequestException
except Exception:
    requests = None
    RequestsException = RuntimeError

logger = get_logger("WebPlayerAdmin")


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
        timeout=HTTP_TIMEOUT_DEFAULT,
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
        timeout=HTTP_TIMEOUT_DEFAULT,
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

    # 回退到 Set-Cookie 头时，只提取 name=value 对，丢弃 Max-Age/Expires/Path 等属性段，
    # 避免把属性写进配置导致 cookie 异常变长（超出 netease.cookie 长度上限）。
    header = response.headers.get("set-cookie", "") if hasattr(response, "headers") else ""
    cookie = _cookie_pairs_from_header(header)
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


__all__ = [
    "_normalize_netease_base_url",
    "_netease_timestamp_params",
    "_netease_api_get",
    "_netease_api_post",
    "_netease_response_data",
    "_netease_qr_code",
    "_cookie_from_response",
    "_netease_login_message",
    "_extract_netease_profile",
    "_netease_account_status",
]
