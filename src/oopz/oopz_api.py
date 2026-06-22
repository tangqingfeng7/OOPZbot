"""Oopz 平台 API Mixin — 域/成员/频道/语音/审核等查询与操作。"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from config import OOPZ_CONFIG
from core.logger_config import get_logger
from oopz.responses import (
    ApiResult,
    MutationOutcome,
    http_error,
    parse_api_response,
    parse_mutation_response,
)

if TYPE_CHECKING:
    import requests as _requests_type

logger = get_logger("OopzApi")


@dataclass(frozen=True)
class RetryPolicy:
    """声明式重试策略（默认只重试一次、不退避）。

    把「按状态码重试 + 退避」从各方法的手写循环里抽出来，新增一个需要限流重试的接口
    只需声明一份策略，而非复制一遍 429 循环。

    backoff: ``attempt(从 1 起) -> 等待秒数``；为 None 时不退避。
    respect_retry_after: 命中 ``Retry-After`` 响应头时优先采用其秒数。
    """

    attempts: int = 1
    statuses: tuple[int, ...] = (429,)
    backoff: Optional[Callable[[int], float]] = None
    respect_retry_after: bool = True

    def wait_seconds(self, resp: "_requests_type.Response", attempt: int) -> float:
        if self.respect_retry_after:
            try:
                retry_after = int(resp.headers.get("Retry-After", "0") or "0")
            except Exception:
                retry_after = 0
            if retry_after > 0:
                return float(retry_after)
        return float(self.backoff(attempt)) if self.backoff else 0.0


# 常用策略：限流（429）退避重试，最多 3 次。
RATE_LIMIT_RETRY = RetryPolicy(attempts=3, backoff=lambda attempt: float(min(attempt, 3)))


class OopzApiMixin:

    # ---- 统一请求层 ----
    #
    # 一个传输执行器（_send）+ 两个响应归一化入口（_query / _mutation）。
    # self._get / self._request 由 OopzSender 提供（带签名、限流、401/403/428 鉴权重试）。
    # 所有业务方法都经由这三者发请求，不再各自手写 try/except、429 循环或 JSON 判定。

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        retry: Optional[RetryPolicy] = None,
    ) -> "_requests_type.Response":
        """已签名 HTTP 请求的唯一传输入口。

        GET 走 ``self._get``（签名含查询参数），其余方法（POST/PUT/DELETE/PATCH）走
        ``self._request``。``retry`` 命中其状态码时按策略退避重试；传输异常向上抛出，
        由 :meth:`_query` / :meth:`_mutation` 统一兜底（异常处理只存在于这两处）。
        """
        method = method.upper()
        attempts = retry.attempts if retry else 1
        resp = None
        for attempt in range(1, attempts + 1):
            if method == "GET":
                resp = self._get(path, params=params)
            else:
                resp = self._request(method, path, body)
            if retry is None or attempt >= attempts or resp.status_code not in retry.statuses:
                return resp
            wait = retry.wait_seconds(resp, attempt)
            logger.warning(
                "%s %s 被限流 HTTP %s，%.1fs 后重试 (%d/%d)",
                method, path, resp.status_code, wait, attempt, attempts,
            )
            if wait > 0:
                time.sleep(wait)
        return resp

    def _query(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        data_key: str = "data",
        data_default: object = None,
        error_with_body: bool = False,
        retry: Optional[RetryPolicy] = None,
    ) -> ApiResult:
        """查询类请求 → :class:`ApiResult`（含传输异常兜底）。"""
        try:
            resp = self._send(method, path, params=params, body=body, retry=retry)
        except Exception as e:
            return ApiResult(False, error=str(e))
        return parse_api_response(
            resp, data_key=data_key, data_default=data_default, error_with_body=error_with_body
        )

    def _mutation(
        self,
        action: str,
        method: str,
        path: str,
        *,
        body: Optional[dict] = None,
        accept_code: bool = False,
        body_limit: int = 200,
        retry: Optional[RetryPolicy] = None,
    ) -> MutationOutcome:
        """变更类请求 → :class:`MutationOutcome`（含传输异常兜底 + 统一原始响应日志）。

        ``action`` 仅用于日志前缀；成功/失败的业务日志与默认文案由调用方决定。
        """
        try:
            resp = self._send(method, path, body=body, retry=retry)
        except Exception as e:
            logger.error("%s请求异常: %s", action, e)
            return MutationOutcome(False, error=str(e))
        logger.info("%s %s %s -> HTTP %s, body: %s", action, method, path, resp.status_code, (resp.text or "")[:300])
        return parse_mutation_response(resp, accept_code=accept_code, body_limit=body_limit)

    # ---- 域成员查询 ----

    def _get_area_members_cache_store(self) -> dict:
        store = getattr(self, "_area_members_cache", None)
        if not isinstance(store, dict):
            store = {}
            self._area_members_cache = store
        return store

    def _get_cached_area_members(
        self,
        cache_key: tuple[str, int, int],
        *,
        max_age: float,
    ) -> Optional[dict]:
        store = self._get_area_members_cache_store()
        cached = store.get(cache_key)
        if not isinstance(cached, dict):
            return None
        ts = cached.get("ts")
        data = cached.get("data")
        if not isinstance(ts, (int, float)) or not isinstance(data, dict):
            return None
        if time.time() - float(ts) > max_age:
            return None
        return copy.deepcopy(data)

    def _set_cached_area_members(self, cache_key: tuple[str, int, int], data: dict) -> None:
        store = self._get_area_members_cache_store()
        max_entries = int(getattr(self, "_cache_max_entries", 200))
        if len(store) >= max_entries:
            oldest = min(store, key=lambda k: store[k].get("ts", 0) if isinstance(store[k], dict) else 0)
            store.pop(oldest, None)
        store[cache_key] = {"ts": time.time(), "data": copy.deepcopy(data)}

    def get_area_members(self, area: Optional[str] = None, offset_start: int = 0, offset_end: int = 49, quiet: bool = False) -> dict:
        """
        获取域内成员列表及在线状态。

        API: GET /area/v3/members?area={area}&offsetStart={start}&offsetEnd={end}

        Args:
            quiet: 为 True 时不向控制台打成功日志（用于轮询等后台调用）。

        Returns:
            {"members": [...], "userCount": int, "onlineCount": int, ...}
            或 {"error": "..."} 表示失败
        """
        area = area or OOPZ_CONFIG["default_area"]
        url_path = "/area/v3/members"
        params = {"area": area, "offsetStart": str(offset_start), "offsetEnd": str(offset_end)}
        cache_key = (str(area), int(offset_start), int(offset_end))
        cache_ttl = float(getattr(self, "_area_members_cache_ttl", 2.0))
        stale_ttl = float(getattr(self, "_area_members_stale_ttl", 300.0))

        if quiet:
            cached = self._get_cached_area_members(cache_key, max_age=cache_ttl)
            if cached is not None:
                return cached

        def _stale_or(error: dict) -> dict:
            """任何失败（限流/非 200/空/解析失败/异常）都优先回退到 stale_ttl 内的缓存。"""
            stale = self._get_cached_area_members(cache_key, max_age=stale_ttl)
            if stale is not None:
                stale["stale"] = True
                return stale
            return error

        try:
            # 限流（429）退避重试统一交给 _send + RATE_LIMIT_RETRY，不再手写循环。
            resp = self._send("GET", url_path, params=params, retry=RATE_LIMIT_RETRY)

            if resp.status_code != 200:
                if resp.status_code == 429:
                    logger.warning(
                        "获取域成员被限流: HTTP 429 (area=%s, offset=%s-%s)", area, offset_start, offset_end
                    )
                else:
                    logger.debug(f"获取域成员失败: HTTP {resp.status_code}")
                fallback = _stale_or({"error": http_error(resp.status_code)})
                if resp.status_code == 429 and fallback.get("stale"):
                    fallback["rateLimited"] = True
                return fallback

            if not resp.content:
                logger.debug("获取域成员失败: HTTP 200 但响应体为空")
                return _stale_or({"error": "empty response"})

            try:
                result = resp.json()
            except ValueError:
                content_encoding = (resp.headers.get("Content-Encoding") or "").lower()
                if content_encoding in ("br", "zstd") or (
                    resp.content and resp.content[:4] != b'{"st'
                ):
                    logger.debug(
                        "获取域成员失败: 响应体可能未被正确解压 "
                        "(Content-Encoding=%s, len=%d)。"
                        "请确保已安装 brotli 和 zstandard 包: "
                        "pip install brotli zstandard",
                        content_encoding or "未知",
                        len(resp.content),
                    )
                else:
                    logger.debug(
                        "获取域成员失败: 响应非合法 JSON (len=%d, status=%d, preview=%r)",
                        len(resp.content),
                        resp.status_code,
                        resp.content[:200],
                    )
                return _stale_or({"error": "invalid JSON"})

            if not result.get("status"):
                msg = result.get("message") or result.get("error") or "未知错误"
                logger.debug(f"获取域成员失败: {msg}")
                return _stale_or({"error": msg})

            data = result.get("data", {})
            members = data.get("members", [])
            online = sum(1 for m in members if m.get("online") == 1)
            fetched = len(members)
            api_total = data.get("totalCount") or data.get("userCount")
            try:
                total = int(api_total) if api_total is not None else fetched
            except Exception:
                total = fetched

            role_count = data.get("roleCount", [])
            online_from_api = sum(
                rc.get("count", 0) for rc in role_count if rc.get("role", 0) != -1
            ) if role_count else online

            if not quiet:
                logger.info(f"获取域成员成功: 本页 {fetched} 人, 在线 {online_from_api} 人, 域总人数 {total}")
            data["onlineCount"] = online_from_api or online
            data["totalCount"] = total
            data["userCount"] = total
            data["fetchedCount"] = fetched
            self._set_cached_area_members(cache_key, data)
            return data
        except Exception as e:
            logger.error(f"获取域成员异常: {e}")
            return _stale_or({"error": str(e)})

    # ---- 频道列表 ----

    def get_area_channels(self, area: Optional[str] = None, quiet: bool = False) -> list:
        """
        获取域内完整频道列表（含分组）。

        API: GET /client/v1/area/v1/detail/v1/channels?area={area}

        Args:
            quiet: 为 True 时不打成功日志（用于轮询等后台调用）。

        Returns:
            频道分组列表，每组含 channels 子列表。失败时返回空列表。
        """
        area = area or OOPZ_CONFIG["default_area"]
        res = self._query("GET", "/client/v1/area/v1/detail/v1/channels", params={"area": area})
        if not res.ok:
            logger.error(f"获取频道列表失败: {res.error}")
            return []
        groups = res.data or []
        if not quiet:
            total = sum(len(g.get("channels") or []) for g in groups)
            logger.info(f"获取频道列表: {total} 个频道, {len(groups)} 个分组")
        return groups

    def get_channel_setting_info(self, channel: str) -> dict:
        """
        获取频道设置详情（名称、访问权限等）。

        API: GET /area/v3/channel/setting/info?channel={channel}
        """
        channel = str(channel or "").strip()
        if not channel:
            return {"error": "缺少 channel"}

        res = self._query("GET", "/area/v3/channel/setting/info", params={"channel": channel}, data_default={})
        if not res.ok:
            logger.error(f"获取频道设置失败: {res.error}")
            return {"error": res.error}
        data = res.data
        if not isinstance(data, dict):
            return {"error": "频道设置响应格式异常"}
        return data

    def _pick_channel_group(
        self,
        area: str,
        preferred_channel: Optional[str] = None,
        preferred_group_name: Optional[str] = None,
    ) -> Optional[str]:
        """优先按分组名匹配，否则选当前频道所在分组，再回退到第一个可用分组。"""
        groups = self.get_area_channels(area=area, quiet=True) or []
        preferred_channel = str(preferred_channel or "").strip()
        preferred_group_name = str(preferred_group_name or "").strip().lower()

        fallback = None
        for group in groups:
            group_id = str(group.get("id") or "").strip()
            if not group_id:
                continue
            group_name = str(group.get("name") or "").strip().lower()
            if not fallback:
                fallback = group_id
            if preferred_group_name and group_name == preferred_group_name:
                return group_id
            channels = group.get("channels") or []
            if preferred_channel and any(str(ch.get("id") or "") == preferred_channel for ch in channels):
                return group_id
        if preferred_group_name:
            return None
        return fallback

    def create_channel(
        self,
        area: Optional[str] = None,
        name: str = "",
        channel_type: str = "text",
        group_id: str = "",
    ) -> dict:
        """
        创建频道（通用方法）。

        Args:
            area: 域 ID
            name: 频道名称
            channel_type: 频道类型 (text/voice)
            group_id: 频道分组 ID，留空自动选择第一个分组
        """
        area = area or OOPZ_CONFIG["default_area"]
        name = str(name or "").strip()
        if not name:
            return {"error": "频道名称不能为空"}

        if not group_id:
            group_id = self._pick_channel_group(area) or ""
            if not group_id:
                return {"error": "未找到可用频道分组"}

        type_map = {"text": "TEXT", "voice": "VOICE", "audio": "VOICE"}
        resolved_type = type_map.get(channel_type.lower(), channel_type.upper())
        body: dict = {
            "area": area,
            "group": group_id,
            "name": name,
            "type": resolved_type,
            "secret": False,
            "maxMember": 100,
        }
        if resolved_type == "VOICE":
            body["isTemp"] = False

        res = self._query("POST", "/client/v1/area/v1/channel/v1/create", body=body, error_with_body=True)
        if not res.ok:
            # res.raw 非空表示是业务 status=false（区别于 HTTP/JSON 传输失败），保留权限提示。
            if res.raw is not None:
                code = res.raw.get("code") or res.raw.get("errorCode") or ""
                logger.warning("创建频道被拒: %s (code=%s), body=%s", res.error, code, body)
                hint = "（可能需要域主/管理员权限）" if "服务" in res.error or "权限" in res.error else ""
                return {"error": f"{res.error}{hint}"}
            return {"error": res.error}

        data = res.data or {}
        channel_id = self._extract_channel_id(data) or self._extract_channel_id(res.raw)
        return {
            "status": True,
            "channel": channel_id or "",
            "name": name,
            "message": "频道已创建",
        }

    def update_channel(
        self,
        area: Optional[str] = None,
        channel_id: str = "",
        overrides: Optional[dict] = None,
        *,
        name: str = "",
    ) -> dict:
        """
        修改频道设置。先拉取当前设置，再合并 *overrides* 中提供的字段后提交。

        API: POST /area/v3/channel/setting/edit

        Args:
            overrides: 要覆盖的字段字典，可包含 name / maxMember /
                       textGapSecond / secret / voiceQuality / voiceDelay 等。
            name: 仅修改名称的快捷参数（向后兼容）。
        """
        area = area or OOPZ_CONFIG["default_area"]
        channel_id = str(channel_id or "").strip()
        if not channel_id:
            return {"error": "缺少 channel_id"}

        setting = self.get_channel_setting_info(channel_id)
        if isinstance(setting, dict) and "error" in setting:
            return {"error": f"获取频道设置失败: {setting['error']}"}

        _BOOL_FIELDS = ("secret", "hasPassword", "voiceControlEnabled",
                        "textControlEnabled", "accessControlEnabled")
        _INT_FIELDS = ("textGapSecond", "maxMember")
        _STR_FIELDS = ("name", "voiceQuality", "voiceDelay", "password")

        edit_body = {
            "channel": channel_id,
            "area": area,
            "name": str(setting.get("name") or ""),
            "textGapSecond": int(setting.get("textGapSecond", 0) or 0),
            "voiceQuality": str(setting.get("voiceQuality") or "64k"),
            "voiceDelay": str(setting.get("voiceDelay") or "LOW"),
            "maxMember": int(setting.get("maxMember", 30000) or 30000),
            "voiceControlEnabled": bool(setting.get("voiceControlEnabled")),
            "textControlEnabled": bool(setting.get("textControlEnabled")),
            "textRoles": list(setting.get("textRoles") or []),
            "voiceRoles": list(setting.get("voiceRoles") or []),
            "accessControlEnabled": bool(setting.get("accessControlEnabled")),
            "accessible": list(setting.get("accessible") or []),
            "accessibleMembers": list(setting.get("accessibleMembers") or []),
            "secret": bool(setting.get("secret")),
            "hasPassword": bool(setting.get("hasPassword")),
            "password": str(setting.get("password") or ""),
        }

        if name:
            edit_body["name"] = name
        if overrides:
            for k, v in overrides.items():
                if k in _INT_FIELDS:
                    edit_body[k] = int(v or 0)
                elif k in _BOOL_FIELDS:
                    edit_body[k] = bool(v)
                elif k in _STR_FIELDS:
                    edit_body[k] = str(v or "")
                elif k in edit_body:
                    edit_body[k] = v

            if "secret" in overrides:
                want_secret = bool(overrides["secret"])
                edit_body["secret"] = want_secret
                edit_body["accessControlEnabled"] = want_secret
                if want_secret:
                    if "accessibleMembers" in overrides and isinstance(overrides["accessibleMembers"], list):
                        members = set(str(u) for u in overrides["accessibleMembers"] if u)
                    else:
                        members = set(edit_body.get("accessibleMembers") or [])
                    bot_uid = str(OOPZ_CONFIG.get("person_uid") or "")
                    if bot_uid:
                        members.add(bot_uid)
                    try:
                        import config as _cfg
                        for uid in getattr(_cfg, "ADMIN_UIDS", []):
                            uid = str(uid).strip()
                            if uid:
                                members.add(uid)
                    except Exception:
                        pass
                    edit_body["accessibleMembers"] = list(members)
                else:
                    edit_body["accessible"] = []
                    edit_body["accessibleMembers"] = []

        out = self._mutation("更新频道", "POST", "/area/v3/channel/setting/edit", body=edit_body)
        if not out.ok:
            return {"error": out.error}
        return {"status": True, "message": "频道已更新"}

    def create_restricted_text_channel(
        self,
        target_uid: str,
        area: Optional[str] = None,
        preferred_channel: Optional[str] = None,
        name: Optional[str] = None,
    ) -> dict:
        """
        创建仅指定成员可见的文字频道。

        先创建频道，再通过 setting/edit 开启访问权限并写入 accessibleMembers。
        """
        area = area or OOPZ_CONFIG["default_area"]
        target_uid = str(target_uid or "").strip()
        if not target_uid:
            return {"error": "缺少 target_uid"}

        group_id = self._pick_channel_group(area, preferred_channel=preferred_channel)
        if not group_id:
            return {"error": "未找到可用频道分组"}

        default_name = f"登录-{target_uid[-4:]}-{time.strftime('%H%M%S')}"
        channel_name = (name or default_name).strip() or "登录"
        body = {
            "area": area,
            "group": group_id,
            "name": channel_name,
            "type": "TEXT",
            "secret": True,
        }

        res = self._query("POST", "/client/v1/area/v1/channel/v1/create", body=body, error_with_body=True)
        if not res.ok:
            return {"error": res.error}

        data = res.data or {}
        channel_id = self._extract_channel_id(data) or self._extract_channel_id(res.raw)
        if not channel_id:
            return {"error": "创建频道成功，但未能提取频道 ID"}

        setting = self.get_channel_setting_info(channel_id)
        if isinstance(setting, dict) and "error" in setting:
            logger.warning("获取新频道设置失败，改用默认值: %s", setting["error"])
            setting = {}

        edit_body = {
            "channel": channel_id,
            "name": str(setting.get("name") or channel_name),
            "textGapSecond": int(setting.get("textGapSecond", 0) or 0),
            "area": area,
            "voiceQuality": str(setting.get("voiceQuality") or "64k"),
            "voiceDelay": str(setting.get("voiceDelay") or "LOW"),
            "maxMember": int(setting.get("maxMember", 30000) or 30000),
            "voiceControlEnabled": bool(setting.get("voiceControlEnabled", False)),
            "textControlEnabled": bool(setting.get("textControlEnabled", False)),
            "textRoles": list(setting.get("textRoles") or []),
            "voiceRoles": list(setting.get("voiceRoles") or []),
            "accessControlEnabled": True,
            "accessible": [],
            "accessibleMembers": [
                uid for uid in dict.fromkeys([
                    str(target_uid),
                    str(OOPZ_CONFIG.get("person_uid") or ""),
                ]) if uid
            ],
            "secret": bool(setting.get("secret", True)),
            "hasPassword": bool(setting.get("hasPassword", False)),
            "password": str(setting.get("password") or ""),
        }

        out = self._mutation("设置受限频道权限", "POST", "/area/v3/channel/setting/edit", body=edit_body)
        if not out.ok:
            self.delete_channel(channel_id, area=area)
            return {"error": out.error}

        logger.info("创建受限频道成功: channel=%s target=%s", channel_id[:24], target_uid[:12])
        return {
            "status": True,
            "channel": channel_id,
            "group": group_id,
            "name": edit_body["name"],
        }

    def delete_channel(self, channel: str, area: Optional[str] = None) -> dict:
        """
        删除频道。

        API: DELETE /client/v1/area/v1/channel/v1/delete?area={area}&channel={channel}
        """
        area = area or OOPZ_CONFIG["default_area"]
        channel = str(channel or "").strip()
        if not channel:
            return {"error": "缺少 channel"}

        url_path = f"/client/v1/area/v1/channel/v1/delete?channel={channel}&area={area}"
        out = self._mutation("删除频道", "DELETE", url_path)
        if not out.ok:
            logger.error("删除频道失败: %s", out.error)
            return {"error": out.error}
        return {"status": True, "message": out.server_message or "已删除频道"}

    # ---- 已加入的域列表 ----

    def get_joined_areas(self, quiet: bool = False) -> list:
        """
        获取当前用户已加入（订阅）的域列表。

        API: GET /userSubscribeArea/v1/list

        Args:
            quiet: 为 True 时不打成功日志（用于轮询等后台调用）。

        Returns:
            域信息列表，每个元素包含 id / code / name / avatar / owner 等字段。
            失败时返回空列表。
        """
        res = self._query("GET", "/userSubscribeArea/v1/list", data_default=[])
        if not res.ok:
            logger.error(f"获取已加入域列表失败: {res.error}")
            return []
        areas = res.data
        if not quiet:
            logger.info(f"获取已加入域列表: {len(areas)} 个域")
            for a in areas:
                logger.info(f"  域: {a.get('name')} (ID={a.get('id')}, code={a.get('code')})")
        return areas

    # ---- 域详情（含频道） ----

    def get_area_info(self, area: Optional[str] = None) -> dict:
        """
        获取域详细信息（含角色列表、主页频道 ID/名称等）。

        API: GET /area/v3/info?area={area}

        Returns:
            域信息字典，或 {"error": "..."} 表示失败。
        """
        area = area or OOPZ_CONFIG["default_area"]
        res = self._query("GET", "/area/v3/info", params={"area": area}, data_default={})
        if not res.ok:
            logger.error(f"获取域详情失败: {res.error}")
            return {"error": res.error}
        return res.data

    def leave_area(self, area: str) -> dict:
        """
        离开指定域。

        API: DELETE /client/v1/area/v1/quit
        """
        area = str(area or "").strip()
        if not area:
            return {"error": "缺少 area"}
        out = self._mutation("离开域", "DELETE", "/client/v1/area/v1/quit", body={"area": area}, accept_code=True)
        if not out.ok:
            return {"error": out.error}
        return {"status": True, "message": out.server_message or "已离开域"}

    # ---- 启动时自动填充域/频道名称 ----

    def populate_names(self):
        """
        从 API 获取已加入的域列表及各域频道列表，
        自动填充 NameResolver 中的域名称和频道名称。
        """
        from oopz.name_resolver import get_resolver
        resolver = get_resolver()

        areas = self.get_joined_areas()
        for a in areas:
            area_id = a.get("id", "")
            area_name = a.get("name", "")
            if area_id and area_name:
                resolver.set_area(area_id, area_name)

            groups = self.get_area_channels(area_id) or []
            for group in groups:
                for ch in (group.get("channels") or []):
                    ch_id = ch.get("id", "")
                    ch_name = ch.get("name", "")
                    if ch_id and ch_name:
                        resolver.set_channel(ch_id, ch_name)

        stats = resolver.get_stats()
        logger.info(
            f"名称自动填充完成: "
            f"{stats['areas_named']} 个域, "
            f"{stats['channels_named']} 个频道"
        )

    # ---- 批量获取用户信息 ----

    def get_person_infos_batch(self, uids: list[str]) -> dict[str, dict]:
        """
        批量获取用户基本信息（昵称、头像、在线状态等）。

        API: POST /client/v1/person/v1/personInfos

        Args:
            uids: 用户 UID 列表

        Returns:
            {uid: {name, avatar, online, pid, ...}, ...}
        """
        if not uids:
            return {}
        result_map: dict[str, dict] = {}
        batch_size = 30
        for i in range(0, len(uids), batch_size):
            batch = uids[i : i + batch_size]
            body = {"persons": batch, "commonIds": []}
            res = self._query("POST", "/client/v1/person/v1/personInfos", body=body, data_default=[])
            if not res.ok:
                logger.debug("批量获取用户信息部分失败: %s", res.error)
                continue
            for person in res.data:
                uid = person.get("uid", "")
                if uid:
                    result_map[uid] = person
        return result_map

    # ---- 好友 / 好友请求 ----

    def get_friendship(self) -> list[dict]:
        """获取好友列表。API: GET /client/v1/list/v1/friendship"""
        res = self._query("GET", "/client/v1/list/v1/friendship", data_default=[])
        if not res.ok:
            logger.error("获取好友列表失败: %s", res.error)
            return []
        data = res.data
        return data if isinstance(data, list) else []

    def get_friendship_requests(self) -> list[dict]:
        """获取好友请求列表。API: GET /client/v1/friendship/v1/requests"""
        res = self._query("GET", "/client/v1/friendship/v1/requests", data_default={})
        if not res.ok:
            logger.error("获取好友请求失败: %s", res.error)
            return []
        data = res.data
        requests = data.get("requests") if isinstance(data, dict) else []
        return requests if isinstance(requests, list) else []

    def post_friendship_response(self, target: str, friend_request_id: int, agree: bool) -> dict:
        """接受或拒绝好友请求。API: POST /client/v1/friendship/v1/response"""
        target = str(target or "").strip()
        if not target:
            return {"error": "缺少 target"}
        body = {"agree": bool(agree), "friendRequestId": int(friend_request_id), "target": target}
        out = self._mutation("处理好友请求", "POST", "/client/v1/friendship/v1/response", body=body, accept_code=True)
        if not out.ok:
            return {"error": out.error}
        return {"status": True, "message": out.server_message or "好友请求已处理"}

    def set_user_remark_name(self, uid: str, remark_name: str = "") -> dict:
        """设置好友备注名。API: POST /person/v1/remarkName/setUserRemarkName"""
        uid = str(uid or "").strip()
        if not uid:
            return {"error": "缺少 uid"}
        body = {"remarkUid": uid, "remarkName": str(remark_name or "")}
        out = self._mutation("设置好友备注", "POST", "/person/v1/remarkName/setUserRemarkName", body=body, accept_code=True)
        if not out.ok:
            return {"error": out.error}
        return {"status": True, "message": out.server_message or "备注已更新"}

    # ---- 个人详细信息 ----

    def get_person_detail(self, uid: Optional[str] = None) -> dict:
        """
        通过 personInfos 接口获取用户信息（可查询任意用户）。

        Args:
            uid: 用户 UID（默认取当前 Bot 自身的 UID）

        Returns:
            包含用户信息的字典，或 {"error": "..."} 表示失败
        """
        uid = uid or OOPZ_CONFIG["person_uid"]
        body = {"persons": [uid], "commonIds": []}

        res = self._query("POST", "/client/v1/person/v1/personInfos", body=body, data_default=[])
        if not res.ok:
            logger.error(f"获取个人信息失败: {res.error}")
            return {"error": res.error}

        data_list = res.data
        if not data_list:
            return {"error": "未找到该用户"}

        person = data_list[0]
        logger.info(f"获取个人信息成功: {person.get('name', '未知')}")
        return person

    # ---- 他人详细资料 ----

    def get_person_detail_full(self, uid: str) -> dict:
        """
        获取他人完整详细资料（比 personInfos 更详细，含 VIP、IP 属地等）。

        API: GET /client/v1/person/v1/personDetail?uid={uid}
        """
        res = self._query("GET", "/client/v1/person/v1/personDetail", params={"uid": uid}, data_default={})
        if not res.ok:
            return {"error": res.error}
        return res.data

    # ---- 自身详细资料 ----

    def get_self_detail(self) -> dict:
        """
        获取当前登录用户的完整详细资料。

        API: GET /client/v1/person/v2/selfDetail?uid={uid}
        """
        uid = OOPZ_CONFIG["person_uid"]
        res = self._query("GET", "/client/v1/person/v2/selfDetail", params={"uid": uid}, data_default={})
        if not res.ok:
            return {"error": res.error}
        return res.data

    # ---- 用户等级信息 ----

    def get_level_info(self) -> dict:
        """
        获取当前用户等级、积分信息。

        API: GET /user_points/v1/level_info

        Returns:
            {"currentLevel": int, "nextLevel": int, "nextLevelDistance": int, ...}
        """
        res = self._query("GET", "/user_points/v1/level_info", data_default={})
        if not res.ok:
            return {"error": res.error}
        return res.data

    # ---- 用户在域内的角色 / 禁言状态 ----

    def get_user_area_detail(self, target: str, area: Optional[str] = None) -> dict:
        """
        获取指定用户在域内的角色列表和禁言/禁麦状态。

        API: GET /area/v3/userDetail?area={area}&target={uid}

        Returns:
            {"list": [{"roleID":..., "name":...}], "disableTextTo":..., "disableVoiceTo":..., "higherUid":...}
        """
        area = area or OOPZ_CONFIG["default_area"]
        res = self._query("GET", "/area/v3/userDetail", params={"area": area, "target": target}, data_default={})
        if not res.ok:
            return {"error": res.error}
        return res.data

    # ---- 可分配的角色列表 ----

    def get_assignable_roles(self, target: str, area: Optional[str] = None) -> list:
        """
        获取当前用户可以分配给目标用户的角色列表。

        API: GET /area/v3/role/canGiveList?area={area}&target={uid}

        Returns:
            [{"roleID": int, "name": str, "owned": bool, "sort": int}, ...]
        """
        area = area or OOPZ_CONFIG["default_area"]
        res = self._query("GET", "/area/v3/role/canGiveList", params={"area": area, "target": target})
        if not res.ok:
            logger.error(f"获取可分配角色失败: {res.error}")
            return []
        data = res.data
        if not isinstance(data, dict):
            return []
        return data.get("roles", [])

    # ---- 给/取消身份组 ----

    def edit_user_role(
        self,
        target_uid: str,
        role_id: int,
        add: bool,
        area: Optional[str] = None,
    ) -> dict:
        """
        给目标用户添加或取消指定身份组。

        真实 API（与 Web 端一致）:
        POST /area/v3/role/editUserRole
        Body: {"area": area, "target": target_uid, "targetRoleIDs": [id1, id2, ...]}
        语义：将目标用户在该域内的身份组设置为 targetRoleIDs 列表（全量覆盖）。

        Args:
            target_uid: 目标用户 UID
            role_id: 身份组 ID（来自 canGiveList 或 userDetail.list）
            add: True=给身份组，False=取消身份组
            area: 域 ID，默认取配置

        Returns:
            {"status": True, "message": "..."} 或 {"error": "..."}
        """
        area = area or OOPZ_CONFIG["default_area"]
        detail = self.get_user_area_detail(target_uid, area=area)
        if "error" in detail:
            return {"error": detail["error"]}
        current_list = detail.get("list") or []
        current_ids = [int(r["roleID"]) for r in current_list if r.get("roleID") is not None]
        role_id = int(role_id)
        if add:
            if role_id not in current_ids:
                current_ids.append(role_id)
        else:
            current_ids = [x for x in current_ids if x != role_id]
        body = {"area": area, "target": target_uid, "targetRoleIDs": current_ids}
        out = self._mutation(f"editUserRole(add={add})", "POST", "/area/v3/role/editUserRole", body=body, body_limit=150)
        if not out.ok:
            return {"error": out.error}
        return {"status": True, "message": out.server_message or ("已给身份组" if add else "已取消身份组")}

    # ---- 搜索域成员 ----

    def search_area_members(self, area: Optional[str] = None, keyword: str = "") -> list:
        """
        搜索域内成员（含角色信息、加入时间）。

        API: POST /area/v3/search/areaSettingMembers

        Returns:
            [{"uid": str, "roleInfos": [...], "enterTime": int}, ...]
        """
        area = area or OOPZ_CONFIG["default_area"]
        body = {"area": area, "name": keyword, "offset": 0, "limit": 50}
        res = self._query("POST", "/area/v3/search/areaSettingMembers", body=body, data_default={})
        if not res.ok:
            logger.error(f"搜索域成员失败: {res.error}")
            return []
        return res.data.get("members", [])

    # ---- 各语音频道在线成员 ----

    _voice_ids_cache: dict = {}

    def _get_voice_channel_ids(self, area: str) -> list[str]:
        """返回域内语音频道 ID 列表，缓存 5 分钟。"""
        cached = self._voice_ids_cache.get(area)
        if cached and time.time() - cached["ts"] < 300:
            return cached["ids"]
        groups = self.get_area_channels(area, quiet=True)
        ids = []
        for g in groups:
            for ch in g.get("channels") or []:
                if str(ch.get("type", "")).upper() in ("VOICE", "AUDIO"):
                    ids.append(ch["id"])
        self._voice_ids_cache[area] = {"ids": ids, "ts": time.time()}
        return ids

    def get_voice_channel_members(self, area: Optional[str] = None) -> dict:
        """
        获取域内各语音频道的在线成员列表。

        API: POST /area/v3/channel/membersByChannels

        Returns:
            {"channelId1": [uid1, uid2, ...], "channelId2": [...], ...}
        """
        area = area or OOPZ_CONFIG["default_area"]
        voice_ids = self._get_voice_channel_ids(area)
        if not voice_ids:
            return {}

        body = {"area": area, "channels": voice_ids}
        retry = RetryPolicy(attempts=3, respect_retry_after=False, backoff=lambda a: float(min(2 ** a, 4)))
        res = self._query("POST", "/area/v3/channel/membersByChannels", body=body, data_default={}, retry=retry)
        if not res.ok:
            logger.error(f"获取语音频道成员失败: {res.error}")
            return {}
        return res.data.get("channelMembers", {}) if isinstance(res.data, dict) else {}

    def get_voice_channel_for_user(self, user_uid: str, area: Optional[str] = None) -> Optional[str]:
        """
        获取用户当前所在的语音频道 ID。
        若用户不在任何语音频道，返回 None。
        """
        members = self.get_voice_channel_members(area=area)
        for ch_id, ch_members in members.items():
            if not ch_members:
                continue
            for m in ch_members:
                uid = m.get("uid", m.get("id", "")) if isinstance(m, dict) else str(m)
                if uid == user_uid:
                    return ch_id
        return None

    def drag_member(
        self,
        target: str,
        to_channel: str,
        from_channel: Optional[str] = None,
        area: Optional[str] = None,
    ) -> dict:
        """
        将用户从其当前语音频道调度（拖拽）到另一个语音频道。

        API: PUT /client/v1/area/v1/member/v1/dragInto
        Body: {"area": area, "channel": from_channel, "toChannel": to_channel, "target": target}
        其中 ``channel`` 为用户当前所在语音频道；未显式提供时自动探测。

        Args:
            target:       被调度用户 UID
            to_channel:   目标语音频道 ID
            from_channel: 用户当前语音频道 ID，留空则自动探测
            area:         域 ID，默认取配置

        Returns:
            {"status": True, "message": "...", "from_channel": str, "to_channel": str}
            或 {"error": "..."}
        """
        area = area or OOPZ_CONFIG["default_area"]
        target = (target or "").strip()
        to_channel = (to_channel or "").strip()
        if not target or not to_channel:
            return {"error": "target 和 toChannel 不能为空"}

        source = (from_channel or "").strip() or self.get_voice_channel_for_user(target, area=area) or ""
        if not source:
            return {"error": "未找到该用户当前所在的语音频道"}
        if source == to_channel:
            return {"error": "用户已在目标语音频道"}

        body = {"area": area, "channel": source, "toChannel": to_channel, "target": target}
        out = self._mutation("语音调度", "PUT", "/client/v1/area/v1/member/v1/dragInto", body=body)
        if not out.ok:
            logger.error("语音调度失败: %s", out.error)
            return {"error": out.error}
        logger.info("语音调度成功: target=%s, %s -> %s", target[:8], source, to_channel)
        return {
            "status": True,
            "message": out.server_message or "已调度",
            "from_channel": source,
            "to_channel": to_channel,
        }

    # ---- 进入域 / 进入频道 ----

    def enter_area(self, area: Optional[str] = None, recover: bool = False) -> dict:
        """
        进入指定域（前置步骤，进入语音频道前需先进入域）。

        API: POST /client/v1/area/v1/enter?area={area}&recover={recover}
        """
        area = area or OOPZ_CONFIG["default_area"]
        url_path = f"/client/v1/area/v1/enter?area={area}&recover={str(recover).lower()}"
        body = {"area": area, "recover": recover}
        res = self._query("POST", url_path, body=body, data_default={})
        if not res.ok:
            logger.error(f"进入域失败: {res.error}")
            return {"error": res.error}
        return res.data

    def enter_channel(self, channel: Optional[str] = None, area: Optional[str] = None,
                      channel_type: str = "TEXT", from_channel: str = "",
                      from_area: str = "", pid: str = "") -> dict:
        """
        进入指定频道（获取频道配置、语音参数、禁言状态等）。

        API: POST /area/v2/channel/enter

        Args:
            channel:      频道 ID
            area:         域 ID
            channel_type: 频道类型，"TEXT" 或 "VOICE"
            from_channel: 切换语音频道时，来源频道 ID
            from_area:    切换语音频道时，来源域 ID
            pid:          语音频道 Agora uid，服务端据此生成 Token

        Returns:
            {"voiceQuality": str, "voiceDelay": str, "disableTextTo": ..., "roleSort": int, ...}
        """
        area = area or OOPZ_CONFIG["default_area"]
        channel = channel or OOPZ_CONFIG["default_channel"]
        url_path = "/area/v2/channel/enter"

        body: dict = {"type": channel_type, "area": area, "channel": channel}
        if channel_type == "VOICE":
            body.update({
                "fromChannel": from_channel,
                "fromArea": from_area,
                "password": "",
                "sign": 1,
                "pid": pid,
            })

        res = self._query("POST", url_path, body=body, data_default={})
        if not res.ok:
            logger.error(f"进入频道失败: {res.error}")
            return {"error": res.error}
        return res.data

    def leave_voice_channel(self, channel: str, area: Optional[str] = None,
                            target: Optional[str] = None) -> dict:
        """
        退出语音频道。

        API: DELETE /client/v1/area/v1/member/v1/removeFromChannel
             ?area={area}&channel={channel}&target={uid}

        Args:
            channel: 语音频道 ID
            area:    域 ID（默认取配置）
            target:  要移出的用户 UID（默认为 Bot 自身）
        """
        area = area or OOPZ_CONFIG["default_area"]
        target = target or OOPZ_CONFIG["person_uid"]
        full_path = f"/client/v1/area/v1/member/v1/removeFromChannel?area={area}&channel={channel}&target={target}"
        out = self._mutation("退出语音频道", "DELETE", full_path)
        if not out.ok:
            logger.error(f"退出语音频道失败: {out.error}")
            return {"error": out.error}
        logger.info("已退出语音频道")
        return {"status": True, "message": "已退出语音频道"}

    # ---- 每日一句 ----

    def get_daily_speech(self) -> dict:
        """
        获取开屏每日一句（名言）。

        Returns:
            {"words": "文本内容", "author": "作者"}
            或 {"error": "..."} 表示失败
        """
        res = self._query("GET", "/general/v1/speech", data_default={})
        if not res.ok:
            logger.error(f"获取每日一句失败: {res.error}")
            return {"error": res.error}
        data = res.data
        logger.info(f"每日一句: {(data or {}).get('words', '')[:30]}...")
        return data

    # ---- 获取频道消息 ----

    def get_channel_messages(
        self,
        area: Optional[str] = None,
        channel: Optional[str] = None,
        size: int = 50,
    ) -> list:
        """
        获取频道最近的消息列表（含 messageId / timestamp / person / content 等）。

        API: GET /im/session/v2/messageBefore?area={area}&channel={channel}&size={size}

        Returns:
            消息列表（按时间倒序，最新在前），失败时返回空列表。
        """
        area = area or OOPZ_CONFIG["default_area"]
        channel = channel or OOPZ_CONFIG["default_channel"]
        params = {"area": area, "channel": channel, "size": str(size)}

        res = self._query("GET", "/im/session/v2/messageBefore", params=params, data_default={})
        if not res.ok:
            logger.error(f"获取频道消息失败: {res.error}")
            return []
        raw_list = res.data.get("messages", []) if isinstance(res.data, dict) else []
        messages = []
        for m in raw_list:
            mid = m.get("messageId") or m.get("id")
            if mid is not None:
                m = {**m, "messageId": str(mid)}
            messages.append(m)
        logger.info(f"获取频道消息: {len(messages)} 条 (area={area[:8]}… channel={channel[:8]}…)")
        return messages

    def find_message_timestamp(
        self,
        message_id: str,
        area: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> Optional[str]:
        """
        从频道最近消息中查找指定 messageId 的 timestamp。
        找不到则返回 None。
        """
        messages = self.get_channel_messages(area=area, channel=channel)
        for msg in messages:
            if msg.get("messageId") == message_id:
                return msg.get("timestamp")
        return None

    # ---- 禁言 / 禁麦 ----
    #
    # 禁言时长 intervalId 映射:
    #   禁言(text): 1=60秒, 2=5分钟, 3=1小时, 4=1天, 5=3天, 6=7天
    #   禁麦(voice): 7=60秒, 8=5分钟, 9=1小时, 10=1天, 11=3天, 12=7天

    _TEXT_INTERVALS = {1: "60秒", 2: "5分钟", 3: "1小时", 4: "1天", 5: "3天", 6: "7天"}
    _VOICE_INTERVALS = {7: "60秒", 8: "5分钟", 9: "1小时", 10: "1天", 11: "3天", 12: "7天"}

    @staticmethod
    def _minutes_to_interval_id(minutes: int, voice: bool = False) -> str:
        """将分钟数映射到最接近的 intervalId。"""
        thresholds = [(1, 7), (5, 8), (60, 9), (1440, 10), (4320, 11), (10080, 12)] if voice \
            else [(1, 1), (5, 2), (60, 3), (1440, 4), (4320, 5), (10080, 6)]
        for limit, iid in thresholds:
            if minutes <= limit:
                return str(iid)
        return str(thresholds[-1][1])

    def mute_user(
        self,
        uid: str,
        area: Optional[str] = None,
        channel: Optional[str] = None,
        duration: int = 10,
    ) -> dict:
        """
        禁言用户（PATCH disableText）。

        Args:
            uid:      目标用户 UID
            area:     区域 ID
            duration: 禁言时长（分钟），自动映射到最近的 intervalId
        """
        area = area or OOPZ_CONFIG["default_area"]
        interval_id = self._minutes_to_interval_id(duration, voice=False)
        url_path = "/client/v1/area/v1/member/v1/disableText"
        query = f"?area={area}&target={uid}&intervalId={interval_id}"
        body = {"area": area, "target": uid, "intervalId": interval_id}
        return self._manage_patch("禁言", url_path, query, body)

    def unmute_user(
        self,
        uid: str,
        area: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> dict:
        """解除禁言（PATCH recoverText）。"""
        area = area or OOPZ_CONFIG["default_area"]
        url_path = "/client/v1/area/v1/member/v1/recoverText"
        query = f"?area={area}&target={uid}"
        body = {"area": area, "target": uid}
        return self._manage_patch("解除禁言", url_path, query, body)

    def mute_mic(
        self,
        uid: str,
        area: Optional[str] = None,
        channel: Optional[str] = None,
        duration: int = 10,
    ) -> dict:
        """禁麦用户（PATCH disableVoice）。"""
        area = area or OOPZ_CONFIG["default_area"]
        interval_id = self._minutes_to_interval_id(duration, voice=True)
        url_path = "/client/v1/area/v1/member/v1/disableVoice"
        query = f"?area={area}&target={uid}&intervalId={interval_id}"
        body = {"area": area, "target": uid, "intervalId": interval_id}
        return self._manage_patch("禁麦", url_path, query, body)

    def unmute_mic(
        self,
        uid: str,
        area: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> dict:
        """解除禁麦（PATCH recoverVoice）。"""
        area = area or OOPZ_CONFIG["default_area"]
        url_path = "/client/v1/area/v1/member/v1/recoverVoice"
        query = f"?area={area}&target={uid}"
        body = {"area": area, "target": uid}
        return self._manage_patch("解除禁麦", url_path, query, body)

    def remove_from_area(
        self,
        uid: str,
        area: Optional[str] = None,
    ) -> dict:
        """
        将用户移出当前域（踢出域）。

        API: POST /area/v3/remove?area={area}&target={uid}
        """
        area = area or OOPZ_CONFIG["default_area"]
        url_path = f"/area/v3/remove?area={area}&target={uid}"
        out = self._mutation("移出域", "POST", url_path, body={"area": area, "target": uid})
        if not out.ok:
            logger.error("移出域失败: %s", out.error)
            return {"error": out.error}
        logger.info("移出域成功")
        return {"status": True, "message": "已移出域"}

    def block_user_in_area(
        self,
        uid: str,
        area: Optional[str] = None,
    ) -> dict:
        """
        封禁用户（加入域封禁列表，用户被踢出且无法再加入）。

        API: DELETE /client/v1/area/v1/block?area={area}&target={uid}
        """
        area = area or OOPZ_CONFIG["default_area"]
        url_path = f"/client/v1/area/v1/block?area={area}&target={uid}"
        out = self._mutation("封禁", "DELETE", url_path)
        if not out.ok:
            logger.error("封禁失败: %s", out.error)
            return {"error": out.error}
        msg = out.server_message or "已封禁"
        logger.info("封禁成功: %s", msg)
        return {"status": True, "message": msg}

    def get_area_blocks(self, area: Optional[str] = None, name: str = "") -> dict:
        """
        获取域内封禁列表。

        API: GET /client/v1/area/v1/areaSettings/v1/blocks?area={area}&name={name}

        Returns:
            {"blocks": [{"uid": "...", ...}, ...]} 或 {"error": "..."}
        """
        area = area or OOPZ_CONFIG["default_area"]
        params = {"area": area, "name": name}

        res = self._query("GET", "/client/v1/area/v1/areaSettings/v1/blocks", params=params, data_default={})
        if not res.ok:
            logger.debug(f"获取域封禁列表失败: {res.error}")
            return {"error": res.error}

        data = res.data
        blocks = data if isinstance(data, list) else data.get("blocks", data.get("list", []))
        if not isinstance(blocks, list):
            blocks = []
        logger.info(f"获取域封禁列表: {len(blocks)} 人")
        return {"blocks": blocks}

    def unblock_user_in_area(
        self,
        uid: str,
        area: Optional[str] = None,
    ) -> dict:
        """
        解除域内封禁（从域封禁列表移除）。

        API: PATCH /client/v1/area/v1/unblock?area={area}&target={uid}
        """
        area = area or OOPZ_CONFIG["default_area"]
        url_path = "/client/v1/area/v1/unblock"
        query = f"?area={area}&target={uid}"
        body = {"area": area, "target": uid}
        return self._manage_patch("解除域内封禁", url_path, query, body)

    def _manage_patch(self, action: str, url_path: str, query: str, body: dict) -> dict:
        """通用 PATCH 管理操作（禁言/禁麦等），参数同时放 query string 和 body。"""
        out = self._mutation(action, "PATCH", url_path + query, body=body)
        if not out.ok:
            logger.error(f"{action}失败: {out.error}")
            return {"error": out.error}
        msg = out.server_message or f"{action}成功"
        logger.info(f"{action}成功: {msg}")
        return {"status": True, "message": msg}

    # ---- 撤回消息 ----

    def recall_message(
        self,
        message_id: str,
        area: Optional[str] = None,
        channel: Optional[str] = None,
        timestamp: Optional[str] = None,
        target: str = "",
    ) -> dict:
        """
        撤回指定消息（需要管理员权限）。

        API: POST /im/session/v1/recallGim
        参数同时放在 query string 和 JSON body 中。

        Args:
            message_id: 消息 ID
            area:       区域 ID（默认取配置）
            channel:    频道 ID（默认取配置）
            timestamp:  消息原始时间戳（微秒），为空则用当前时间
            target:     目标用户 UID（撤回他人消息时填写，默认空）
        """
        area = area or OOPZ_CONFIG["default_area"]
        channel = channel or OOPZ_CONFIG["default_channel"]
        timestamp = timestamp or self.signer.timestamp_us()
        message_id = str(message_id).strip() if message_id is not None else ""

        full_path = (
            f"/im/session/v1/recallGim?area={area}&channel={channel}"
            f"&messageId={message_id}&timestamp={timestamp}&target={target}"
        )
        body = {
            "area": area,
            "channel": channel,
            "messageId": message_id,
            "timestamp": timestamp,
            "target": target,
        }
        out = self._mutation("撤回", "POST", full_path, body=body, accept_code=True)
        if not out.ok:
            logger.error(f"撤回消息失败: {out.error}")
            return {"error": out.error}
        logger.info(f"撤回消息成功: {message_id}")
        return {"status": True, "message": "撤回成功"}

    def recall_private_message(
        self,
        message_id: str,
        *,
        channel: str = "",
        target: str = "",
        area: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> dict:
        """
        撤回私信消息。

        API: POST /im/session/v1/recallIm
        """
        message_id = str(message_id or "").strip()
        target = str(target or "").strip()
        channel = str(channel or "").strip()
        if not message_id:
            return {"error": "缺少 message_id"}
        if not target:
            return {"error": "缺少 target"}
        if not channel:
            opened = self.open_private_session(target)
            if "error" in opened:
                return opened
            channel = str(opened.get("channel") or "")
        if not channel:
            return {"error": "私信 channel 不可用"}

        timestamp = timestamp or self.signer.timestamp_us()
        body = {
            "area": area,
            "channel": channel,
            "messageId": message_id,
            "timestamp": timestamp,
            "target": target,
        }
        out = self._mutation("撤回私信", "POST", "/im/session/v1/recallIm", body=body, accept_code=True)
        if not out.ok:
            return {"error": out.error}
        return {"status": True, "message": out.server_message or "撤回成功"}
