"""插件共享的 JSON HTTP 客户端基类。

统一封装「带重试的 JSON 请求」样板：User-Agent 头、超时、代理、重试循环，
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import aiohttp

from core.async_http import ManagedHttpClient
from core.constants import USER_AGENT
from core.logger_config import get_logger


class JsonHttpClient:
    """带重试与 JSON 解析的 HTTP 客户端基类。

    子类可覆盖 ``_LOG_NAME`` 以保留各自的日志通道。``request_json`` 的返回契约：
    成功返回解析后的 JSON；网络异常重试耗尽后返回 ``{"_error": last_error}``；
    JSON 解析失败立即返回 ``{"_error": ...}``。
    """

    _LOG_NAME = "PluginHttp"

    def __init__(
        self,
        *,
        session=None,
        timeout: int = 15,
        retries: int = 2,
        proxies: dict | None = None,
        proxy_value: object = None,
    ) -> None:
        self._http = session or ManagedHttpClient(proxy_value=proxy_value)
        self._timeout = max(1, int(timeout))
        self._retries = max(1, int(retries))
        self._proxies = proxies
        self._logger = get_logger(self._LOG_NAME)

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: Any = None,
        headers: dict | None = None,
        on_status: Callable[[int], Any] | None = None,
    ) -> Any:
        """发起带重试的 JSON 请求。

        ``on_status`` 可在 ``raise_for_status`` 之前拦截特定状态码：返回非 None
        即作为最终结果直接返回（用于 404/429 等需要自定义文案的场景）。
        """
        merged_headers = {"User-Agent": USER_AGENT}
        if headers:
            merged_headers.update(headers)
        last_error = ""
        for attempt in range(1, self._retries + 1):
            try:
                status, payload = await self._http.request_payload(
                    method,
                    url,
                    params=params or {},
                    json_body=json,
                    headers=merged_headers,
                    timeout=self._timeout,
                    raise_for_status=False,
                )
                if on_status is not None:
                    intercepted = on_status(status)
                    if intercepted is not None:
                        return intercepted
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}")
                return payload
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError) as exc:
                last_error = str(exc)
                self._logger.warning("%s %s attempt %d: %s", method, url, attempt, exc)
            except ValueError as exc:
                return {"_error": f"JSON 解析失败: {exc}"}
            if attempt < self._retries:
                await asyncio.sleep(min(attempt, 3))
        return {"_error": last_error}

    async def close(self) -> None:
        closer = getattr(self._http, "close", None)
        if closer is not None:
            await closer()
