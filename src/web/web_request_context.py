"""Web 层请求上下文辅助 —— 判定请求的原始协议，并据此决定 Cookie 的 Secure 属性。

反代（nginx）场景下 uvicorn 实际收到的永远是明文 http，``request.url.scheme``
不可信，必须优先看 nginx 透传的 ``X-Forwarded-Proto``（见 nginx/nginx.conf）。
反过来，纯 HTTP 部署里若仍按配置给 Cookie 打上 Secure，浏览器不会回传该
Cookie：播放器表现为链接反复失效，管理后台表现为「登录接口返回 200 但下一个
请求就被判为未登录」。

判定逻辑集中在此，播放器与管理后台共用同一份实现。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅用于类型标注：运行时不引入 fastapi，便于独立单测
    from fastapi import Request


def request_is_https(request: "Request") -> bool:
    """请求的原始协议是否为 HTTPS（优先信任反代透传的 X-Forwarded-Proto）。"""
    proto = request.headers.get("x-forwarded-proto", "")
    if proto:
        return proto.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def cookie_secure_for(request: "Request", configured: bool) -> bool:
    """仅当配置要求 Secure 且当前请求确为 HTTPS 时才返回 True。

    ``configured`` 由调用点现取（如 ``cfg.admin_cookie_secure()``），
    本模块不缓存任何配置值。
    """
    return bool(configured) and request_is_https(request)


__all__ = [
    "request_is_https",
    "cookie_secure_for",
]
