from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from core.async_http import ManagedHttpClient
from core.logger_config import get_logger

logger = get_logger("DeltaForceApi")


class DeltaForceApiClient:
    """Thin HTTP client with retry and base URL failover."""

    def __init__(self, config: dict, session=None) -> None:
        self._config = config or {}
        self._api_key = str(self._config.get("api_key") or "").strip()
        self._client_id = str(self._config.get("client_id") or "").strip()
        self._api_mode = str(self._config.get("api_mode") or "auto").strip().lower()
        raw_urls = self._config.get("base_urls") or []
        if not isinstance(raw_urls, list):
            raw_urls = []
        self._base_urls = [str(url).rstrip("/") for url in raw_urls if str(url).strip()]
        if not self._base_urls:
            self._base_urls = [
                "https://df-api-eo.shallow.ink",
                "https://df-api-esa.shallow.ink",
                "https://df-api.shallow.ink",
            ]
        self._timeout = max(1, int(self._config.get("request_timeout_sec", 30) or 30))
        self._retries = max(1, int(self._config.get("request_retries", 3) or 3))
        self._http = session or ManagedHttpClient(proxy_value=self._config.get("proxy"))

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._client_id)

    @property
    def client_id(self) -> str:
        return self._client_id

    def _candidate_bases(self) -> list[str]:
        if self._api_mode == "auto":
            return list(self._base_urls)
        for base in self._base_urls:
            label = base.rsplit("//", 1)[-1].split(".", 1)[0].rsplit("-", 1)[-1]
            if label == self._api_mode:
                return [base]
        return [self._base_urls[0]]

    def _transport_error(self, message: str) -> dict:
        return {
            "success": False,
            "_transport_error": True,
            "message": message,
        }

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        json_mode: bool = False,
    ) -> dict:
        if not self.configured:
            return self._transport_error("插件未配置 api_key 或 client_id")

        params = params or {}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
        }
        if json_mode:
            headers["Content-Type"] = "application/json"

        errors: list[str] = []
        http_method = method.upper().strip() or "GET"
        for base_url in self._candidate_bases():
            url = f"{base_url}{path}"
            for attempt in range(1, self._retries + 1):
                try:
                    status, body = await self._http.request_payload(
                        http_method,
                        url,
                        params=params if http_method == "GET" else None,
                        json_body=params if http_method != "GET" and json_mode else None,
                        data=params if http_method != "GET" and not json_mode else None,
                        headers=headers,
                        timeout=self._timeout,
                        raise_for_status=False,
                    )
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
                    errors.append(f"{base_url}#{attempt}: {exc}")
                    if attempt < self._retries:
                        await asyncio.sleep(min(attempt, 3))
                    continue

                if status >= 500:
                    errors.append(f"{base_url}#{attempt}: HTTP {status}")
                    if attempt < self._retries:
                        await asyncio.sleep(min(attempt, 3))
                    continue

                if status >= 400 and not body:
                    return {
                        "success": False,
                        "code": status,
                        "message": f"HTTP {status}",
                    }
                return body if isinstance(body, dict) else {"success": False, "message": "API 返回格式错误"}

        logger.warning("DeltaForceApi: request failed for %s: %s", path, " | ".join(errors))
        return self._transport_error("后端接口暂时不可用，请稍后再试")

    async def close(self) -> None:
        closer = getattr(self._http, "close", None)
        if closer is not None:
            await closer()

    async def get_login_qr(self, platform: str) -> dict:
        return await self._request(f"/login/{platform}/qr")

    async def get_login_status(self, platform: str, framework_token: str) -> dict:
        return await self._request(
            f"/login/{platform}/status",
            {"frameworkToken": framework_token},
        )

    async def bind_user(
        self,
        framework_token: str,
        platform_id: str,
        client_id: str | None = None,
        client_type: str = "oopz",
    ) -> dict:
        return await self._request(
            "/user/bind",
            {
                "frameworkToken": framework_token,
                "platformID": platform_id,
                "clientID": client_id or self._client_id,
                "clientType": client_type,
            },
            method="POST",
        )

    async def get_user_list(
        self,
        platform_id: str,
        client_id: str | None = None,
        client_type: str = "oopz",
    ) -> dict:
        return await self._request(
            "/user/list",
            {
                "platformID": platform_id,
                "clientID": client_id or self._client_id,
                "clientType": client_type,
            },
        )

    async def bind_character(self, framework_token: str) -> dict:
        return await self._request(
            "/df/person/bind",
            {"frameworkToken": framework_token, "method": "bind"},
        )

    async def get_personal_info(self, framework_token: str) -> dict:
        return await self._request(
            "/df/person/personalInfo",
            {"frameworkToken": framework_token},
        )

    async def get_daily_record(self, framework_token: str, mode: str = "", date: str = "") -> dict:
        params: dict[str, Any] = {"frameworkToken": framework_token}
        if mode:
            params["type"] = mode
        if date:
            params["date"] = date
        return await self._request("/df/person/dailyRecord", params)

    async def get_weekly_record(
        self,
        framework_token: str,
        mode: str = "",
        date: str = "",
        show_extra: bool = False,
        show_null_friend: bool = True,
    ) -> dict:
        params: dict[str, Any] = {
            "frameworkToken": framework_token,
            "isShowNullFriend": str(bool(show_null_friend)).lower(),
        }
        if mode:
            params["type"] = mode
        if date:
            params["date"] = date
        if show_extra:
            params["showExtra"] = "true"
        return await self._request("/df/person/weeklyRecord", params)

    async def get_record(self, framework_token: str, type_id: int, page: int) -> dict:
        return await self._request(
            "/df/person/record",
            {
                "frameworkToken": framework_token,
                "type": int(type_id),
                "page": max(1, int(page)),
            },
        )

    async def get_friend_info(self, framework_token: str, openid: str) -> dict:
        return await self._request(
            "/df/person/friendInfo",
            {
                "frameworkToken": framework_token,
                "friend_openid": openid,
            },
        )

    async def get_collection(self, framework_token: str) -> dict:
        return await self._request(
            "/df/person/collection",
            {"frameworkToken": framework_token},
        )

    async def get_collection_map(self) -> dict:
        return await self._request("/df/object/collection")

    async def get_daily_keyword(self) -> dict:
        return await self._request("/df/tools/dailykeyword")

    async def get_money(self, framework_token: str) -> dict:
        return await self._request(
            "/df/person/money",
            {"frameworkToken": framework_token},
        )

    async def get_ban_history(self, framework_token: str) -> dict:
        return await self._request(
            "/login/qqsafe/ban",
            {"frameworkToken": framework_token},
        )

    async def get_place_status(self, framework_token: str) -> dict:
        return await self._request(
            "/df/place/status",
            {"frameworkToken": framework_token},
        )

    async def get_title(self, framework_token: str) -> dict:
        return await self._request(
            "/df/person/title",
            {"frameworkToken": framework_token},
        )

    async def get_personal_data(self, framework_token: str, mode: str = "", season_id: str = "all") -> dict:
        params: dict[str, Any] = {"frameworkToken": framework_token}
        if mode:
            params["type"] = mode
        if season_id and season_id != "all":
            params["seasonid"] = season_id
        return await self._request("/df/person/personalData", params)

    async def get_red_list(self, framework_token: str) -> dict:
        return await self._request(
            "/df/person/redlist",
            {"frameworkToken": framework_token},
        )

    async def get_red_record(self, framework_token: str, object_id: str) -> dict:
        return await self._request(
            "/df/person/redone",
            {"frameworkToken": framework_token, "objectid": object_id},
        )

    async def get_object_list(self, primary: str = "", second: str = "") -> dict:
        params: dict[str, Any] = {}
        if primary:
            params["primary"] = primary
        if second:
            params["second"] = second
        return await self._request("/df/object/list", params)

    async def search_object(self, name: str = "", ids: str = "") -> dict:
        params: dict[str, Any] = {}
        if name:
            params["name"] = name
        if ids:
            params["id"] = ids
        return await self._request("/df/object/search", params)

    async def get_solution_list(
        self,
        framework_token: str,
        platform_id: str,
        weapon_name: str = "",
        price_range: str = "",
        *,
        client_id: str | None = None,
        client_type: str = "oopz",
    ) -> dict:
        params: dict[str, Any] = {
            "clientID": client_id or self._client_id,
            "clientType": client_type,
            "platformID": platform_id,
            "frameworkToken": framework_token,
        }
        if weapon_name:
            params["weaponName"] = weapon_name
        if price_range:
            params["priceRange"] = price_range
        return await self._request("/df/tools/solution/v2/list", params)

    async def get_solution_detail(
        self,
        framework_token: str,
        platform_id: str,
        solution_id: str,
        *,
        client_id: str | None = None,
        client_type: str = "oopz",
    ) -> dict:
        return await self._request(
            "/df/tools/solution/v2/detail",
            {
                "clientID": client_id or self._client_id,
                "clientType": client_type,
                "platformID": platform_id,
                "frameworkToken": framework_token,
                "solutionId": str(solution_id),
            },
        )

    async def get_price_history_v1(self, object_id: str) -> dict:
        return await self._request(
            "/df/object/price/history/v1",
            {"id": str(object_id)},
        )

    async def get_price_history_v2(self, object_id: str) -> dict:
        return await self._request(
            "/df/object/price/history/v2",
            {"objectId": str(object_id)},
        )


def describe_common_failure(payload: dict | None) -> str | None:
    """Map common API failures to user-facing Chinese text."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    data_dict = data if isinstance(data, dict) else {}
    if payload.get("_transport_error"):
        return str(payload.get("message") or "后端接口暂时不可用，请稍后再试")

    code = str(payload.get("code", ""))
    message = str(
        payload.get("message")
        or payload.get("msg")
        or payload.get("error")
        or payload.get("sMsg")
        or ""
    )

    if code in {"1000", "1001"}:
        return "API Key 无效或已过期，请联系管理员检查插件配置。"
    if code == "1100":
        return "API Key 权限不足，当前账号订阅等级不支持该功能。"
    if data_dict.get("ret") == 101 or "请先完成QQ或微信登录" in message or "请先登录" in message:
        return "当前登录状态已失效，请重新执行三角洲登录。"
    if data_dict.get("ret") == 99998 or "先绑定大区" in message:
        return "请先完成角色绑定后再查询。"
    if payload.get("success") is False and ("未找到有效token" in message or "缺少frameworkToken参数" in message):
        return "当前激活账号无效，请重新登录或切换账号。"
    if payload.get("success") is False and message:
        return f"操作失败：{message}"
    return None
