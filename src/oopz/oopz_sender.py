from __future__ import annotations

import random
import re
import threading
import time
from typing import Optional, Protocol, TypeGuard

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa

from config import DEFAULT_HEADERS, OOPZ_CONFIG

try:
    from config import AUTO_RECALL_CONFIG
except ImportError:
    AUTO_RECALL_CONFIG = {"enabled": False}
from core.constants import UID_PATTERN
from core.http_constants import HTTP_TIMEOUT_API, HTTP_TIMEOUT_LOGIN, HttpTimeout
from core.json_utils import compact_json
from core.logger_config import get_logger
from core.proxy_utils import configure_requests_session
from oopz.oopz_api import OopzApiMixin
from oopz.oopz_upload import UploadMixin, get_image_info  # noqa: F401 — re-export
from oopz.responses import http_error
from oopz.signing import oopz_auth_headers, rsa_sign

logger = get_logger("OopzSender")

# 平台内容审核拒绝时的关键词（命中即视为风控拦截，区别于普通发送失败）
_SENSITIVE_REJECTION_KEYWORDS = ("敏感", "违规", "涉政", "涉黄")


class AutoRecallScheduler(Protocol):
    """发送器依赖的最小自动撤回调度契约。"""

    def schedule_recall(
        self,
        message_id: str,
        channel: str,
        area: str,
        timestamp: str = "",
        *,
        delay: float,
    ) -> bool:
        ...


class SensitiveContentError(RuntimeError):
    """消息被平台内容审核（风控）拦截。区别于网络/参数等普通发送失败，
    便于上层将其降级为可忽略的 WARNING，而非误报为解析/处理异常。"""


def _is_sensitive_rejection(message: str) -> bool:
    text = str(message or "")
    return any(kw in text for kw in _SENSITIVE_REJECTION_KEYWORDS)


class ClientMessageIdGenerator:
    """生成 15 位客户端消息 ID（模拟真实格式）"""

    def generate(self) -> str:
        timestamp_us = int(time.time() * 1_000_000)
        base_id = timestamp_us % 10_000_000_000_000
        suffix = random.randint(10, 99)
        return str(base_id * 100 + suffix)


# ---------------------------------------------------------------------------
# RSA 签名器
# ---------------------------------------------------------------------------

class Signer:
    """Oopz API 请求签名器"""

    def __init__(self):
        self.private_key = self._load_key()
        self.id_gen = ClientMessageIdGenerator()

    # -- 密钥加载 --

    @staticmethod
    def _load_key():
        try:
            from private_key import get_private_key
            return get_private_key()
        except (ImportError, Exception):
            # 临时密钥能让进程起来（测试与本地开发需要），但服务端不认它 ——
            # 之后每一个接口都会 401/403。排查「全部接口都失败」时这条是第一现场，
            # 所以用 error 级并写明后果，不能只 warning 一句「不可用」。
            logger.error("=" * 50)
            logger.error("private_key.py 不可用，已回落到临时生成的随机密钥。")
            logger.error("该密钥服务端不认，所有 Oopz 接口都会返回 401/403。")
            logger.error("请复制 private_key.example.py 为 private_key.py，")
            logger.error("或用账号密码登录（OOPZ_CONFIG.login_phone/login_password）自动写入。")
            logger.error("=" * 50)
            return rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
                backend=default_backend(),
            )

    # -- ID / 时间戳 --

    @staticmethod
    def timestamp_us() -> str:
        return str(int(time.time() * 1_000_000))

    def client_message_id(self) -> str:
        return self.id_gen.generate()

    # -- 签名 --

    def sign(self, data: str) -> str:
        """RSA PKCS1v15 + SHA256 签名，返回 Base64"""
        return rsa_sign(self.private_key, data)

    def oopz_headers(self, url_path: str, body_str: str) -> dict[str, str]:
        """
        构造 Oopz 专用签名请求头。
        签名流程: MD5(url_path + body_json) + timestamp → RSA 签名
        """
        return oopz_auth_headers(self.private_key, OOPZ_CONFIG, url_path, body_str)


# ---------------------------------------------------------------------------
# 消息发送器
# ---------------------------------------------------------------------------

class OopzSender(UploadMixin, OopzApiMixin):
    """Oopz 平台消息发送、文件上传、平台 API 查询。"""

    # 全局速率限制: 最小请求间隔 (秒), 即 1/max_rps
    _RATE_LIMIT_INTERVAL = 0.35  # ~3 req/s
    _AUTH_REFRESH_STATUSES = {401, 403, 428}

    def __init__(self):
        self.signer = Signer()
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._proxy_value = OOPZ_CONFIG.get("proxy")
        self._area_members_cache: dict[tuple[str, int, int], dict] = {}
        self._area_members_cache_ttl = 15.0
        self._area_members_stale_ttl = 300.0
        self._cache_max_entries = 200
        self._rate_lock = threading.Lock()
        self._auth_refresh_lock = threading.Lock()
        self._last_request_time = 0.0
        self._auto_recall_scheduler: AutoRecallScheduler | None = None
        self._auto_recall_unbound_warned = False
        # 代理：留空/不设=使用系统代理(HTTP_PROXY/HTTPS_PROXY)；False 或 "direct"=直连；或 "http://ip:port"
        proxy_settings = configure_requests_session(self.session, self._proxy_value)
        proxy_cfg = proxy_settings.server or ""
        if proxy_settings.mode == "direct":
            logger.info("OopzSender: 已禁用代理（直连）")
        elif proxy_settings.enabled:
            logger.info(f"OopzSender: 使用代理 {proxy_cfg.strip()}")
        # 否则使用 requests 默认行为（读取环境变量）

        logger.info("OopzSender 已初始化")
        logger.info(f"  用户: {OOPZ_CONFIG['person_uid']}")
        logger.info(f"  设备: {OOPZ_CONFIG['device_id']}")

    def bind_auto_recall_scheduler(self, scheduler: AutoRecallScheduler) -> None:
        """绑定应用级共享撤回调度器，避免为每条消息创建独立 Timer。"""
        self._auto_recall_scheduler = scheduler

    def _throttle(self) -> None:
        """阻塞直到距上次请求满足最小间隔,线程安全。"""
        with self._rate_lock:
            now = time.time()
            elapsed = now - self._last_request_time
            if elapsed < self._RATE_LIMIT_INTERVAL:
                time.sleep(self._RATE_LIMIT_INTERVAL - elapsed)
            self._last_request_time = time.time()

    # ---- 内部 ----

    def _signed_request_once(
        self,
        method: str,
        url_path: str,
        body: dict | None = None,
        *,
        timeout: HttpTimeout = None,
    ) -> requests.Response:
        """统一处理带签名的 HTTP 请求（POST/PUT/DELETE/PATCH）。

        timeout 留空时用 HTTP_TIMEOUT_API。确实慢的接口由调用方显式传更长档位，
        不要为了个别接口把默认档一刀切调大 —— 没有超时时服务端挂住会无限阻塞
        调用线程，dispatcher 只有 4 个 worker，卡满即全线失联。
        """
        self._throttle()
        if body is not None:
            body_str = compact_json(body)
            sign_str = body_str
            data = body_str.encode("utf-8")
        elif method.upper() in ("POST", "PUT"):
            sign_str = "{}"
            data = b"{}"
        else:
            sign_str = ""
            data = None
        headers = {**self.session.headers, **self.signer.oopz_headers(url_path, sign_str)}
        url = OOPZ_CONFIG["base_url"] + url_path
        return self.session.request(
            method,
            url,
            headers=headers,
            data=data,
            timeout=timeout if timeout is not None else HTTP_TIMEOUT_API,
        )

    def _refresh_credentials_after_auth_failure(self, status_code: int) -> bool:
        with self._auth_refresh_lock:
            from oopz.oopz_password_login import (
                OopzPasswordLoginError,
                load_private_key_from_pem,
                refresh_credentials_from_config_password,
            )

            try:
                credentials = refresh_credentials_from_config_password(timeout=HTTP_TIMEOUT_LOGIN, save=True)
            except OopzPasswordLoginError as exc:
                logger.warning("OOPZ 登录态失效，自动刷新失败，继续使用现有凭据: HTTP %s, %s", status_code, exc)
                return False
            except Exception as exc:
                logger.warning("OOPZ 登录态失效，自动刷新异常，继续使用现有凭据: HTTP %s, %s", status_code, exc, exc_info=True)
                return False

            if not credentials:
                logger.warning(
                    "OOPZ 登录态失效: HTTP %s。未配置 login_phone/login_password 或 phone/password，无法自动刷新凭据。",
                    status_code,
                )
                return False

            pem = str(credentials.get("private_key_pem") or "").strip()
            if pem:
                try:
                    self.signer.private_key = load_private_key_from_pem(pem)
                except Exception as exc:
                    logger.warning("OOPZ 登录态已刷新，但更新签名私钥失败: %s", exc, exc_info=True)
                    return False

            cache = getattr(self, "_area_members_cache", None)
            if isinstance(cache, dict):
                cache.clear()
            logger.info("OOPZ 登录态已自动刷新，正在重试刚才失败的请求")
            return True

    def _request(
        self, method: str, url_path: str, body: dict | None = None,
        *, timeout: HttpTimeout = None,
    ) -> requests.Response:
        """统一处理带签名的 HTTP 请求（POST/PUT/DELETE/PATCH）。"""
        resp = self._signed_request_once(method, url_path, body, timeout=timeout)
        if resp.status_code in self._AUTH_REFRESH_STATUSES and self._refresh_credentials_after_auth_failure(resp.status_code):
            resp = self._signed_request_once(method, url_path, body, timeout=timeout)
        return resp

    def _post(self, url_path: str, body: dict, *, timeout: HttpTimeout = None) -> requests.Response:
        return self._request("POST", url_path, body, timeout=timeout)

    def _put(self, url_path: str, body: dict, *, timeout: HttpTimeout = None) -> requests.Response:
        return self._request("PUT", url_path, body, timeout=timeout)

    def _delete(self, url_path: str, body: Optional[dict] = None, *, timeout: HttpTimeout = None) -> requests.Response:
        """DELETE 请求，部分撤回接口可能用 DELETE 方法"""
        return self._request("DELETE", url_path, body, timeout=timeout)

    def _get_once(self, url_path: str, params: Optional[dict] = None, *, timeout: HttpTimeout = None) -> requests.Response:
        """GET 请求（签名包含查询参数）。"""
        self._throttle()
        if params:
            from urllib.parse import urlencode
            sign_path = url_path + "?" + urlencode(params)
        else:
            sign_path = url_path

        headers = {**self.session.headers, **self.signer.oopz_headers(sign_path, "")}
        url = OOPZ_CONFIG["base_url"] + url_path
        return self.session.get(
            url, headers=headers, params=params,
            timeout=timeout if timeout is not None else HTTP_TIMEOUT_API,
        )

    def _get(self, url_path: str, params: Optional[dict] = None, *, timeout: HttpTimeout = None) -> requests.Response:
        """GET 请求（签名包含查询参数）。"""
        resp = self._get_once(url_path, params=params, timeout=timeout)
        if resp.status_code in self._AUTH_REFRESH_STATUSES and self._refresh_credentials_after_auth_failure(resp.status_code):
            resp = self._get_once(url_path, params=params, timeout=timeout)
        return resp

    # ---- 发送消息 ----

    @staticmethod
    def _resolve_default_style_tags(area: str) -> list[str]:
        """根据"域级覆盖 + 全局默认"决定 styleTags。

        全局默认来自 OOPZ_CONFIG.use_announcement_style；某个域可以在 AreaConfig 里
        显式打开/关闭，覆盖全局值。
        """
        fallback = bool(OOPZ_CONFIG.get("use_announcement_style", False))
        try:
            from core.area_config import get_area_registry
            use_announcement = get_area_registry().get_announcement_style(area, fallback)
        except Exception:
            use_announcement = fallback
        return ["IMPORTANT"] if use_announcement else []

    def send_message(
        self,
        text: str,
        area: Optional[str] = None,
        channel: Optional[str] = None,
        auto_recall: Optional[bool] = None,
        **kwargs,
    ) -> requests.Response:
        """
        发送聊天消息。

        Args:
            text:    消息文本
            area:    区域 ID（默认取配置）
            channel: 频道 ID（默认取配置）
            auto_recall: 是否自动撤回。None=按配置决定，False=不撤回，True=强制撤回
            **kwargs: attachments, mentionList, referenceMessageId, styleTags 等。styleTags 默认由"域级 use_announcement_style"覆盖"全局 OOPZ_CONFIG.use_announcement_style"决定（开启=["IMPORTANT"]）
        """
        area = area or OOPZ_CONFIG["default_area"]
        channel = channel or OOPZ_CONFIG["default_channel"]
        default_style = self._resolve_default_style_tags(area)

        body = {
            "area": area,
            "channel": channel,
            "target": kwargs.get("target", ""),
            "clientMessageId": self.signer.client_message_id(),
            "timestamp": self.signer.timestamp_us(),
            "isMentionAll": kwargs.get("isMentionAll", False),
            "mentionList": kwargs.get("mentionList", []),
            "styleTags": kwargs.get("styleTags", default_style),
            "referenceMessageId": kwargs.get("referenceMessageId", None),
            "animated": kwargs.get("animated", False),
            "displayName": kwargs.get("displayName", ""),
            "duration": kwargs.get("duration", 0),
            "text": text,
            "attachments": kwargs.get("attachments", []),
        }

        url_path = "/im/session/v1/sendGimMessage"
        logger.info(f"发送消息: {text[:80]}{'...' if len(text) > 80 else ''}")

        try:
            resp = self._post(url_path, body)
            logger.info(f"响应状态: {resp.status_code}")
            if resp.text:
                logger.debug(f"响应内容: {resp.text[:200]}")
            result = resp.json()
            if not result.get("status") and result.get("code") not in (0, "0", 200, "200", "success"):
                err = result.get("message") or result.get("error") or str(result)
                if _is_sensitive_rejection(err):
                    logger.warning(
                        f"消息被平台风控拦截（{err}），已跳过: "
                        f"{text[:60]}{'...' if len(text) > 60 else ''}"
                    )
                    raise SensitiveContentError(err)
                raise RuntimeError(f"send_message failed: {err}")
            if auto_recall is not False:
                self._schedule_auto_recall(resp, area, channel)
            return resp
        except SensitiveContentError:
            raise
        except Exception as e:
            logger.error(f"发送失败: {e}")
            raise

    def send_to_default(self, text: str, **kwargs) -> requests.Response:
        """发送到默认频道"""
        return self.send_message(text, **kwargs)

    # ---- 私信 ----

    @staticmethod
    def _looks_like_private_channel(value: object) -> TypeGuard[str]:
        """
        判断字符串是否像 Oopz 私信 channel。

        已知示例类似 ULID：01KJP5MHQC7TSQ6FDKT8N1DZAX
        避免把 32 位小写十六进制 UID 误判成 channel。
        """
        if not isinstance(value, str):
            return False
        text = value.strip()
        if not text:
            return False
        if re.fullmatch(UID_PATTERN, text):
            return False
        return bool(re.fullmatch(r"[0-9A-Z]{20,40}", text))

    @classmethod
    def _find_private_channel_candidate(cls, payload: object) -> Optional[str]:
        """递归扫描响应体，寻找像私信 channel 的字符串。"""
        if isinstance(payload, dict):
            for value in payload.values():
                found = cls._find_private_channel_candidate(value)
                if found:
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = cls._find_private_channel_candidate(item)
                if found:
                    return found
        elif cls._looks_like_private_channel(payload):
            return str(payload).strip()
        return None

    @staticmethod
    def _extract_private_channel(payload: object) -> Optional[str]:
        """从私信会话接口响应中尽量提取 channel。"""
        if isinstance(payload, dict):
            for key in (
                "channel",
                "chatChannel",
                "sessionChannel",
                "channelId",
                "chatChannelId",
                "sessionId",
                "imChannel",
                "imSessionChannel",
                "conversationId",
                "id",
            ):
                value = payload.get(key)
                if OopzSender._looks_like_private_channel(value):
                    return value.strip()
            for key in (
                "data",
                "result",
                "session",
                "chat",
                "conversation",
                "conversationInfo",
                "chatInfo",
                "currentSession",
                "imSession",
            ):
                nested = payload.get(key)
                found = OopzSender._extract_private_channel(nested)
                if found:
                    return found
            sessions = payload.get("sessions") or payload.get("list")
            if isinstance(sessions, list):
                for item in sessions:
                    found = OopzSender._extract_private_channel(item)
                    if found:
                        return found
            found = OopzSender._find_private_channel_candidate(payload)
            if found:
                return found
        elif isinstance(payload, list):
            for item in payload:
                found = OopzSender._extract_private_channel(item)
                if found:
                    return found
        return None

    @staticmethod
    def _extract_channel_id(payload: object) -> Optional[str]:
        """从通用响应中提取频道 ID（文字频道 / 私信 channel 均适用）。"""
        return OopzSender._extract_private_channel(payload)

    @staticmethod
    def _short_payload(payload: object, limit: int = 240) -> str:
        """将响应体压缩为短日志文本。"""
        try:
            text = compact_json(payload)
        except Exception:
            text = str(payload)
        return text[:limit]

    @staticmethod
    def _validate_private_send_result(result: object) -> tuple[bool, str]:
        """
        判断 sendImMessage 的业务响应是否明确成功。

        
        """
        if not isinstance(result, dict):
            return False, "响应不是 JSON 对象"

        if "status" in result:
            if result.get("status") is True:
                return True, ""
            return False, str(result.get("message") or result.get("error") or "status=false")

        if "success" in result:
            if result.get("success") is True:
                return True, ""
            return False, str(result.get("message") or result.get("msg") or result.get("error") or "success=false")

        if "code" in result:
            code = result.get("code")
            if code in (0, "0", ""):
                # code=0 仍不够稳，至少需要有 data / result / messageId 之一
                if any(key in result for key in ("data", "result", "messageId")):
                    return True, ""
                return False, "code=0 但无明确投递确认字段"
            return False, str(result.get("message") or result.get("msg") or result.get("error") or f"code={code}")

        if any(key in result for key in ("data", "result", "messageId")):
            return True, ""

        return False, "HTTP 200 但响应未明确确认私信已发送"

    def open_private_session(self, target: str) -> dict:
        """
        打开或创建与指定用户的私信会话。

        API: PATCH /client/v1/chat/v1/to?target={uid}
        """
        target = str(target or "").strip()
        if not target:
            return {"error": "缺少 target"}

        url_path = "/client/v1/chat/v1/to"
        query = f"?target={target}"
        full_path = url_path + query
        body = {"target": target}

        try:
            # 走统一签名通道 _request，复用 401/403/428 登录态自动刷新与重试。
            resp = self._request("PATCH", full_path, body)
        except Exception as e:
            logger.error(f"打开私信会话异常: {e}")
            return {"error": str(e)}

        raw = resp.text or ""
        logger.info(f"打开私信会话 PATCH {full_path} -> HTTP {resp.status_code}, body: {raw[:300]}")

        if resp.status_code != 200:
            return {"error": http_error(resp.status_code, raw)}

        try:
            result = resp.json()
        except Exception:
            return {"error": f"响应非 JSON: {raw[:200]}"}

        channel = self._extract_private_channel(result)
        if not channel:
            logger.error("打开私信会话成功但未提取到 channel，响应: %s", self._short_payload(result))
            return {
                "error": "未能从响应中提取私信 channel",
                "raw": result,
                "debug_reason": "open_session_missing_channel",
            }
        return {"status": True, "channel": channel, "raw": result}

    def send_private_message(
        self,
        target: str,
        text: str,
        *,
        attachments: Optional[list] = None,
        style_tags: Optional[list] = None,
        channel: Optional[str] = None,
    ) -> dict:
        """
        发送私信消息。

        API: POST /im/session/v2/sendImMessage
        """
        target = str(target or "").strip()
        if not target:
            return {"error": "缺少 target"}

        if not channel:
            opened = self.open_private_session(target)
            if "error" in opened:
                return opened
            channel = opened.get("channel")

        if not channel:
            logger.error("发送私信失败：私信 channel 不可用 (target=%s)", target[:12])
            return {"error": "私信 channel 不可用", "debug_reason": "missing_channel"}

        # Web 端格式：请求体为 { "message": { ... } }，正文用 content（Playwright 抓包确认）
        body = {
            "message": {
                "area": "",
                "channel": channel,
                "target": target,
                "clientMessageId": self.signer.client_message_id(),
                "timestamp": self.signer.timestamp_us(),
                "isMentionAll": False,
                "mentionList": [],
                "styleTags": style_tags if style_tags is not None else [],
                "referenceMessageId": None,
                "animated": False,
                "displayName": "",
                "duration": 0,
                "content": text,
                "attachments": attachments or [],
            }
        }
        url_path = "/im/session/v2/sendImMessage"

        try:
            # 走统一签名通道 _request，复用 401/403/428 登录态自动刷新与重试。
            resp = self._request("POST", url_path, body)
        except Exception as e:
            logger.error(f"发送私信异常: {e}")
            return {"error": str(e)}

        raw = resp.text or ""
        logger.info(f"发送私信 POST {url_path} -> HTTP {resp.status_code}, body: {raw[:300]}")

        if resp.status_code != 200:
            logger.error("发送私信失败：HTTP %s，响应: %s", resp.status_code, raw[:240])
            return {
                "error": http_error(resp.status_code, raw),
                "channel": channel,
                "debug_reason": "send_dm_http_error",
            }

        try:
            result = resp.json()
        except Exception:
            result = {"raw": raw}

        ok, reason = self._validate_private_send_result(result)
        if not ok:
            logger.error("发送私信未获确认: %s, 响应: %s", reason, self._short_payload(result))
            return {
                "error": f"HTTP 200 但未确认发送成功: {reason}",
                "channel": channel,
                "result": result,
                "debug_reason": "send_dm_unconfirmed",
            }

        logger.info("发送私信成功: channel=%s", str(channel)[:24])
        return {"status": True, "channel": channel, "result": result}

    # ---- 自动撤回 ----

    def _schedule_auto_recall(self, resp: requests.Response, area: str, channel: str):
        """根据配置，在延迟后自动撤回刚发送的消息。"""
        if not AUTO_RECALL_CONFIG.get("enabled"):
            return
        delay = AUTO_RECALL_CONFIG.get("delay", 30)
        if delay <= 0:
            return

        try:
            result = resp.json()
            data = result.get("data", {})
            msg_id = None
            if isinstance(data, dict):
                msg_id = data.get("messageId")
            if not msg_id:
                msg_id = result.get("messageId")
            if not msg_id:
                logger.debug("自动撤回: 无法从响应中提取 messageId，跳过")
                return
            msg_id = str(msg_id)

            scheduler = self._auto_recall_scheduler
            if scheduler is None:
                if not self._auto_recall_unbound_warned:
                    logger.warning("自动撤回调度器尚未绑定，已跳过自动撤回任务")
                    self._auto_recall_unbound_warned = True
                return
            if scheduler.schedule_recall(
                message_id=msg_id,
                channel=channel,
                area=area,
                delay=float(delay),
            ):
                logger.debug(f"已安排 {delay}s 后自动撤回: {msg_id[:16]}...")
        except Exception as e:
            logger.debug(f"安排自动撤回失败: {e}")

    # ---- 批量 ----

    def send_multiple(self, messages: list[str], interval: float = 1.0) -> list[dict]:
        """批量发送消息"""
        results = []
        for i, msg in enumerate(messages, 1):
            try:
                resp = self.send_to_default(msg)
                results.append({"message": msg, "status_code": resp.status_code, "success": resp.status_code == 200})
                if i < len(messages):
                    time.sleep(interval)
            except Exception as e:
                results.append({"message": msg, "status_code": None, "success": False, "error": str(e)})
        return results
