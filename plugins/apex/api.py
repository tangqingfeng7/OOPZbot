"""Apex Legends API 客户端 (数据源: apexlegendsapi.com)。"""

from __future__ import annotations

from typing import Any, Optional

import requests

from core.logger_config import get_logger
from plugins._shared.http_client import JsonHttpClient

logger = get_logger("ApexApi")

_BASE_URL = "https://api.mozambiquehe.re"

_PLATFORM_ALIASES: dict[str, str] = {
    "pc": "PC",
    "origin": "PC",
    "steam": "PC",
    "ps": "PS4",
    "ps4": "PS4",
    "ps5": "PS4",
    "playstation": "PS4",
    "xbox": "X1",
    "x1": "X1",
    "xb": "X1",
    "switch": "SWITCH",
    "ns": "SWITCH",
}


def normalize_platform(raw: str) -> str:
    """将用户输入的平台字符串标准化为 API 所需的格式。"""
    return _PLATFORM_ALIASES.get(raw.strip().lower(), "PC")


class ApexApiClient(JsonHttpClient):
    """apexlegendsapi.com 非官方 API 封装。

    需要在 https://portal.apexlegendsapi.com/ 免费注册获取 API Key。
    """

    _LOG_NAME = "ApexApi"

    def __init__(self, config: dict, session: Optional[requests.Session] = None) -> None:
        self._config = config or {}
        self._api_key = str(self._config.get("api_key") or "").strip()
        super().__init__(
            session=session,
            timeout=int(self._config.get("request_timeout_sec", 15) or 15),
            retries=int(self._config.get("request_retries", 2) or 2),
        )

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @staticmethod
    def _intercept_status(code: int) -> Any:
        if code == 404:
            return {"_error": "玩家未找到，请检查名称和平台是否正确。", "_code": 404}
        if code == 429:
            return {"_error": "API 请求频率超限，请稍后再试。", "_code": 429}
        return None

    def _get(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> Any:
        url = f"{_BASE_URL}/{endpoint.lstrip('/')}"
        params = dict(params or {})
        params["auth"] = self._api_key
        return self.request_json(
            "GET",
            url,
            params=params,
            headers={"Authorization": self._api_key},
            on_status=self._intercept_status,
        )

    def get_player(self, player: str, platform: str = "PC") -> dict:
        """查询玩家统计数据。"""
        return self._get("bridge", params={
            "player": player,
            "platform": normalize_platform(platform),
            "merge": "true",
            "removeMerged": "true",
        })

    def get_player_by_uid(self, uid: str, platform: str = "PC") -> dict:
        """通过 UID 查询玩家统计数据。"""
        return self._get("bridge", params={
            "uid": uid,
            "platform": normalize_platform(platform),
            "merge": "true",
            "removeMerged": "true",
        })

    def get_map_rotation(self) -> dict:
        """获取当前地图轮换信息。"""
        return self._get("maprotation", params={"version": "2"})

    def get_crafting_rotation(self) -> list | dict:
        """获取当前复制器合成轮换。"""
        return self._get("crafting")

    def get_predator(self) -> dict:
        """获取当前赛季猎杀者门槛。"""
        return self._get("predator")

    def get_news(self, lang: str = "en-US") -> list | dict:
        """获取最新 Apex 新闻。"""
        return self._get("news", params={"lang": lang})

    def get_server_status(self) -> dict:
        """获取服务器状态。"""
        return self._get("servers")
