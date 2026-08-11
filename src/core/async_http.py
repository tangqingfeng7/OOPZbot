"""应用共享的可关闭异步 HTTP 会话。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse
from urllib.request import proxy_bypass

import aiohttp
from aiohttp_socks import ProxyConnector

from core.proxy_utils import ProxySettings, resolve_proxy_settings


class ManagedHttpClient:
    """按项目 direct/system/HTTP/SOCKS 语义复用一个 ClientSession。"""

    def __init__(self, *, proxy_value: object = None, headers: dict[str, str] | None = None) -> None:
        self._settings: ProxySettings = resolve_proxy_settings(proxy_value)
        self._headers = dict(headers or {})
        self._session: aiohttp.ClientSession | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = None
            if self._settings.enabled and str(self._settings.scheme or "").startswith("socks"):
                connector = ProxyConnector.from_url(str(self._settings.server))
            self._session = aiohttp.ClientSession(
                headers=self._headers,
                connector=connector,
                trust_env=self._settings.mode == "system",
            )
        return self._session

    def request_proxy(self, url: str) -> str | None:
        if not self._settings.enabled:
            return None
        if str(self._settings.scheme or "").startswith("socks"):
            return None
        host = (urlparse(url).hostname or "").strip()
        if host and proxy_bypass(host):
            return None
        return self._settings.server

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> dict[str, Any] | None:
        _status, payload = await self.request_payload(
            method,
            url,
            params=params,
            data=data,
            json_body=json_body,
            headers=headers,
            timeout=timeout,
        )
        return payload if isinstance(payload, dict) else None

    async def request_payload(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float,
        raise_for_status: bool = True,
    ) -> tuple[int, Any]:
        session = await self.session()
        async with session.request(
            method,
            url,
            params=params,
            data=data,
            json=json_body,
            headers=headers,
            proxy=self.request_proxy(url),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if raise_for_status:
                response.raise_for_status()
            payload = await response.json(content_type=None)
            return response.status, payload

    async def request_text(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float,
        raise_for_status: bool = True,
    ) -> tuple[int, str]:
        session = await self.session()
        async with session.request(
            method,
            url,
            params=params,
            headers=headers,
            proxy=self.request_proxy(url),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if raise_for_status:
                response.raise_for_status()
            return response.status, await response.text()

    async def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()


__all__ = ["ManagedHttpClient"]
