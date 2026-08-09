"""Web 层请求上下文辅助 —— 判定请求的原始协议，并据此决定 Cookie 的 Secure 属性。

反代（nginx）场景下 uvicorn 实际收到的永远是明文 http，``request.url.scheme``
不可信，必须优先看 nginx 透传的 ``X-Forwarded-Proto``（见 nginx/nginx.conf）。
反过来，纯 HTTP 部署里若仍按配置给 Cookie 打上 Secure，浏览器不会回传该
Cookie：播放器表现为链接反复失效，管理后台表现为「登录接口返回 200 但下一个
请求就被判为未登录」。

判定逻辑集中在此，播放器与管理后台共用同一份实现。
"""

from __future__ import annotations

from typing import Protocol

from web.web_rate_limit import ClientAddressRequest, ClientAddressResolver


class RequestUrl(Protocol):
    @property
    def scheme(self) -> str: ...


class RequestContext(ClientAddressRequest, Protocol):
    """协议判定所需的最小请求接口。"""

    @property
    def url(self) -> RequestUrl: ...


def request_is_https(request: RequestContext, trusted_proxy_cidrs=()) -> bool:
    """仅在 TCP 对端可信时接受 ``X-Forwarded-Proto``。"""
    proto = request.headers.get("x-forwarded-proto", "")
    client = request.client
    peer = client.host if client else ""
    resolver = ClientAddressResolver(tuple(trusted_proxy_cidrs or ()))
    if proto and resolver.is_trusted(peer):
        normalized = proto.strip().lower()
        if normalized in {"http", "https"}:
            return normalized == "https"
    return request.url.scheme == "https"


def cookie_secure_for(
    request: RequestContext,
    configured: bool,
    trusted_proxy_cidrs=(),
) -> bool:
    """仅当配置要求 Secure 且当前请求确为 HTTPS 时才返回 True。

    ``configured`` 由调用点现取（如 ``cfg.admin_cookie_secure()``），
    本模块不缓存任何配置值。
    """
    return bool(configured) and request_is_https(request, trusted_proxy_cidrs)


__all__ = [
    "RequestContext",
    "RequestUrl",
    "cookie_secure_for",
    "request_is_https",
]
