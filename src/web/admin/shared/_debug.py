"""跨平台登录相关的通用工具：Cookie 整理、敏感信息遮罩与调试摘要。"""

from __future__ import annotations

import re
from http.cookies import SimpleCookie
from typing import Optional

from core.logger_config import get_logger

logger = get_logger("WebPlayerAdmin")

# Set-Cookie 属性段关键字（小写）：提取 name=value 时需要丢弃这些非 Cookie 字段。
_COOKIE_ATTR_NAMES = {
    "expires",
    "max-age",
    "domain",
    "path",
    "secure",
    "httponly",
    "samesite",
    "comment",
    "version",
    "priority",
    "partitioned",
}


def _cookie_pairs_from_header(header: str, allowed_names: tuple[str, ...] = ()) -> str:
    """从原始 Set-Cookie 头提取 name=value 对，丢弃 Expires/Max-Age/Path 等属性段。

    兼容多 Cookie 折叠（逗号分隔）与 Expires 日期自带逗号的情况：按 ``;`` 和 ``,`` 拆分后，
    只保留形如 name=value 且 name 不是属性关键字的片段；传入 allowed_names 时仅保留白名单内的 Cookie。
    """
    allowed = set(allowed_names)
    pairs: list[str] = []
    for segment in re.split(r"[;,]", header or ""):
        segment = segment.strip()
        if "=" not in segment:
            continue
        name, _, value = segment.partition("=")
        name = name.strip()
        value = value.strip()
        if not name or not value:
            continue
        if allowed:
            if name not in allowed:
                continue
        elif name.lower() in _COOKIE_ATTR_NAMES:
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _cookie_pairs_from_response(response, allowed_names: tuple[str, ...] = ()) -> str:
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


__all__ = [
    "_cookie_pairs_from_response",
    "_cookie_pairs_from_header",
    "_mask_debug_token",
    "_cookie_debug_summary",
    "_debug_profile_text",
]
