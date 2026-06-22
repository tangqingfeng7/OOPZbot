"""插件共享的 JSON HTTP 客户端基类。

统一封装「带重试的 JSON 请求」样板：User-Agent 头、超时、代理、重试循环，
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import requests

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
        session: Optional[requests.Session] = None,
        timeout: int = 15,
        retries: int = 2,
        proxies: Optional[dict] = None,
    ) -> None:
        self._session = session or requests.Session()
        self._timeout = max(1, int(timeout))
        self._retries = max(1, int(retries))
        self._proxies = proxies
        self._logger = get_logger(self._LOG_NAME)

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        json: Any = None,
        headers: Optional[dict] = None,
        on_status: Optional[Callable[[int], Any]] = None,
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
                resp = self._session.request(
                    method,
                    url,
                    params=params or {},
                    json=json,
                    headers=merged_headers,
                    timeout=self._timeout,
                    proxies=self._proxies,
                )
                if on_status is not None:
                    intercepted = on_status(resp.status_code)
                    if intercepted is not None:
                        return intercepted
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_error = str(exc)
                self._logger.warning("%s %s attempt %d: %s", method, url, attempt, exc)
            except ValueError as exc:
                return {"_error": f"JSON 解析失败: {exc}"}
        return {"_error": last_error}
