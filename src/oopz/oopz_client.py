import base64
import json
import os
import time
import threading
from typing import Callable, Optional

import websocket

from config import OOPZ_CONFIG, DEFAULT_HEADERS
from core.logger_config import get_logger
from oopz.name_resolver import get_resolver
from oopz.oopz_sender import SensitiveContentError
from core.proxy_utils import get_websocket_proxy_kwargs

logger = get_logger("OopzClient")

_json_loads = json.loads

OOPZ_WS_URL = "wss://ws.oopz.cn"

# Oopz WebSocket 事件类型
EVENT_SERVER_ID = 1
EVENT_CHAT_MESSAGE = 9
EVENT_AUTH = 253
EVENT_HEARTBEAT = 254

# 连接存活超过该时长才视为「健康会话」，重连退避计数才会清零。
# 防止认证被拒等「TCP 能通但立刻被断」的场景把退避重置成 2s 高频重试。
_HEALTHY_SESSION_SECONDS = 60.0

# JWT 剩余有效期低于该值就提前刷新，避免用临期 token 建连后很快失效。
_JWT_REFRESH_MARGIN_SECONDS = 60.0


def _jwt_expires_in(token: str) -> Optional[float]:
    """解析 JWT 的 exp 字段，返回距过期的秒数（负数=已过期）。

    解析失败返回 None（视为未知，不据此阻止连接）。仅做 base64 解码，
    不校验签名——这里只需要客户端侧的过期预判。
    """
    try:
        parts = str(token or "").split(".")
        if len(parts) != 3:
            return None
        payload_raw = parts[1]
        payload_raw += "=" * (-len(payload_raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_raw.encode("ascii")))
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return None
        return float(exp) - time.time()
    except Exception:
        return None


# 认证响应中视为「显式失败」的 code 取值（Oopz HTTP API 惯用
# code="success" 表示成功，因此这里用失败白名单而非成功白名单，
# 未知取值一律不判失败，绝不误杀健康连接）。
_AUTH_FAILURE_CODES = frozenset({
    "401", "403", "428", "fail", "failed", "error", "unauthorized",
    "auth_failed", "invalid_token", "token_expired", "token_invalid",
})


def _auth_response_failed(body: dict) -> bool:
    """从认证响应 body 中保守地识别失败标记。

    仅在出现显式失败信号时返回 True，避免对未知协议格式误判。
    """
    if not isinstance(body, dict):
        return False
    if body.get("success") is False or body.get("status") is False or body.get("ok") is False:
        return True
    if body.get("error"):
        return True
    code = body.get("code")
    if code is not None and str(code).strip().lower() in _AUTH_FAILURE_CODES:
        return True
    return False

# 设置 OOPZ_DEBUG_WS_EVENTS=1 打开 WS 收到事件的原始 body 调试日志，
# 默认关闭以避免高频事件刷屏。诊断语音状态广播、成员变更等问题时再打开。
_DEBUG_WS_EVENTS = os.environ.get("OOPZ_DEBUG_WS_EVENTS", "").strip().lower() in ("1", "true", "yes", "on")


class OopzClient:
    """
    Oopz WebSocket 客户端，支持自动重连。

    用法::

        client = OopzClient(on_chat_message=my_handler)
        client.start()          # 阻塞运行
        # 或
        client.start_async()    # 后台线程运行
    """

    def __init__(
        self,
        on_chat_message: Optional[Callable[[dict], None]] = None,
        on_other_event: Optional[Callable[[int, dict], None]] = None,
        on_raw_event: Optional[Callable[[dict], None]] = None,
        reconnect_interval: float = 2.0,
        max_reconnect_interval: float = 120.0,
        heartbeat_interval: float = 10.0,
        stale_connection_timeout: float = 90.0,
        credential_refresher: Optional[Callable[[], Optional[dict]]] = None,
        min_credential_refresh_interval: float = 300.0,
    ):
        self.on_chat_message = on_chat_message
        self.on_other_event = on_other_event
        self.on_raw_event = on_raw_event
        self._base_reconnect = reconnect_interval
        self._max_reconnect = max_reconnect_interval
        self.heartbeat_interval = heartbeat_interval
        # 超过该时长没收到任何服务端数据就判定连接失效（半开/静默断网），
        # 主动断开触发重连。0 或负数 = 关闭该检测。
        self.stale_connection_timeout = float(stale_connection_timeout or 0)
        # 凭据刷新回调：返回含 person_uid/device_id/jwt_token 的 dict 或 None。
        self._credential_refresher = credential_refresher
        self._min_refresh_interval = float(min_credential_refresh_interval)
        self._last_refresh_attempt = 0.0

        self._person_id = OOPZ_CONFIG["person_uid"]
        self._device_id = OOPZ_CONFIG["device_id"]
        self._jwt_token = OOPZ_CONFIG["jwt_token"]

        self._ws: Optional[websocket.WebSocketApp] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._consecutive_failures = 0
        self._fail_lock = threading.Lock()
        self._hb_body = json.dumps({"person": self._person_id})
        self._last_recv_time = 0.0
        self._session_started_at = 0.0

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def _next_reconnect_delay(self) -> float:
        with self._fail_lock:
            delay = min(
                self._base_reconnect * (2 ** self._consecutive_failures),
                self._max_reconnect,
            )
            self._consecutive_failures += 1
        return delay

    def start(self):
        """阻塞运行（带指数退避自动重连）"""
        self._running = True
        while self._running:
            self._maybe_refresh_expired_credentials()
            try:
                self._connect_and_run()
            except Exception as e:
                logger.error(f"WebSocket 异常: {e}")

            self._reset_backoff_if_session_healthy()

            if self._running:
                delay = self._next_reconnect_delay()
                logger.info(f"{delay:.1f}s 后重连 (第 {self._consecutive_failures} 次)")
                time.sleep(delay)

    def _reset_backoff_if_session_healthy(self) -> None:
        """只有存活足够久的会话才清零退避计数：TCP 能建立但立刻被
        服务端断开（如认证被拒）时保持指数退避，避免 2s 高频重试。"""
        if self._session_started_at and (
            time.time() - self._session_started_at >= _HEALTHY_SESSION_SECONDS
        ):
            with self._fail_lock:
                self._consecutive_failures = 0

    def start_async(self):
        """在后台线程中运行"""
        self._thread = threading.Thread(target=self.start, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self):
        """停止客户端"""
        self._running = False
        if self._ws:
            self._ws.close()

    def update_credentials(self, person_uid: str, device_id: str, jwt_token: str, reconnect: bool = True) -> None:
        """热更新认证凭据；关闭当前连接后由重连循环使用新身份。"""
        self._person_id = str(person_uid or "")
        self._device_id = str(device_id or "")
        self._jwt_token = str(jwt_token or "")
        self._hb_body = json.dumps({"person": self._person_id})
        if reconnect and self._ws:
            try:
                self._ws.close()
            except Exception:
                logger.debug("热更新 OOPZ 凭据时关闭旧 WebSocket 失败", exc_info=True)

    def _maybe_refresh_expired_credentials(self) -> bool:
        """连接前预检 JWT：已过期或临近过期时尝试通过回调刷新凭据。"""
        expires_in = _jwt_expires_in(self._jwt_token)
        if expires_in is None or expires_in > _JWT_REFRESH_MARGIN_SECONDS:
            return False
        if self._credential_refresher is None:
            logger.warning(
                "OOPZ JWT 已过期或即将过期（%.0fs），且未配置凭据刷新回调，连接可能被拒绝",
                expires_in,
            )
            return False
        return self._try_refresh_credentials(f"JWT 剩余 {expires_in:.0f}s")

    def _try_refresh_credentials(self, reason: str) -> bool:
        """节流地调用凭据刷新回调并应用结果。

        JWT 预检与认证被拒两条路径共享同一节流窗口（默认 5 分钟），
        防止凭据坏死时高频请求登录接口。返回 True 表示已应用新凭据。
        """
        if self._credential_refresher is None:
            return False
        now = time.time()
        if now - self._last_refresh_attempt < self._min_refresh_interval:
            return False
        self._last_refresh_attempt = now
        logger.info("尝试自动刷新 OOPZ WS 凭据（%s）...", reason)
        try:
            credentials = self._credential_refresher()
        except Exception:
            logger.warning("OOPZ 凭据刷新回调异常", exc_info=True)
            return False
        if not isinstance(credentials, dict):
            logger.warning("OOPZ 凭据刷新未返回有效凭据，继续使用现有凭据")
            return False
        person = str(credentials.get("person_uid") or self._person_id)
        device = str(credentials.get("device_id") or self._device_id)
        token = str(credentials.get("jwt_token") or "")
        if not (person and device and token):
            logger.warning("OOPZ 凭据刷新结果缺少必要字段，继续使用现有凭据")
            return False
        self.update_credentials(person, device, token, reconnect=False)
        logger.info("OOPZ WS 凭据已自动刷新")
        return True

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _connect_and_run(self):
        """建立一次 WebSocket 连接并持续运行直到断开"""
        ws_headers = {
            "User-Agent": DEFAULT_HEADERS["User-Agent"],
            "Origin": DEFAULT_HEADERS["Origin"],
            "Cache-Control": DEFAULT_HEADERS["Cache-Control"],
            "Accept-Language": DEFAULT_HEADERS["Accept-Language"],
            "Accept-Encoding": DEFAULT_HEADERS["Accept-Encoding"],
        }

        self._ws = websocket.WebSocketApp(
            OOPZ_WS_URL,
            header=ws_headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

        logger.info(f"正在连接 {OOPZ_WS_URL} ...")
        self._session_started_at = time.time()
        self._last_recv_time = time.time()
        # 协议层 ping 保持关闭（Oopz 服务端对 RFC ping/pong 的支持未验证），
        # 半开连接的检测由 _heartbeat_loop 的收包水位（stale_connection_timeout）负责。
        self._ws.run_forever(
            ping_interval=0,
            ping_timeout=None,
            **get_websocket_proxy_kwargs(OOPZ_CONFIG.get("proxy")),
        )

    # -- WebSocket 回调 --

    def _on_open(self, ws):
        logger.info("WebSocket 连接已建立")
        self._last_recv_time = time.time()
        self._send_auth(ws)
        threading.Thread(target=self._heartbeat_loop, args=(ws,), daemon=True).start()

    def _on_message(self, ws, message: str):
        self._last_recv_time = time.time()
        try:
            data = _json_loads(message)
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"无法解析消息: {message[:200]}")
            return

        event = data.get("event")
        if self.on_raw_event:
            try:
                self.on_raw_event(data)
            except Exception:
                logger.warning("on_raw_event 处理异常（OneBot 等旁路可能漏事件）", exc_info=True)

        if _DEBUG_WS_EVENTS and event != EVENT_HEARTBEAT:
            try:
                preview = message[:600]
            except Exception:
                preview = "<unprintable>"
            logger.debug(f"[WS recv] event={event} body={preview}")

        # 心跳响应 -- 最高频事件，优先处理
        if event == EVENT_HEARTBEAT:
            body_raw = data.get("body", {})
            if isinstance(body_raw, str):
                try:
                    body = _json_loads(body_raw)
                except (json.JSONDecodeError, ValueError):
                    body = {}
            elif isinstance(body_raw, dict):
                body = body_raw
            else:
                body = {}
            if body.get("r") == 1:
                self._send_heartbeat(ws)
            return

        # 服务端 serverId 确认
        if event == EVENT_SERVER_ID:
            self._send_heartbeat(ws)
            logger.info("收到 serverId，已发送首次心跳")
            return

        # 认证响应：记录结果并识别显式失败（凭据过期/被拒）
        if event == EVENT_AUTH:
            self._handle_auth_response(ws, data)
            return

        # 聊天消息
        if event == EVENT_CHAT_MESSAGE:
            self._handle_chat(data)
            return

        # 其他事件（如域成员加入/退出等）交给外部处理
        if self.on_other_event:
            try:
                self.on_other_event(event, data)
            except Exception:
                logger.warning("on_other_event 处理异常", exc_info=True)

    def _on_error(self, ws, error):
        logger.error(f"WebSocket 错误: {error}")

    def _on_close(self, ws, code, reason):
        logger.warning(f"连接关闭 (code={code}, reason={reason})")

    def _handle_auth_response(self, ws, data: dict) -> None:
        """处理服务端认证响应（event 253）。

        协议无公开文档，这里只对显式失败标记做判定：失败时告警并
        尝试刷新凭据、断开重连；无法判定时仅记录，不影响连接。
        """
        body = self._safe_json_parse(data.get("body", {}))
        if not _auth_response_failed(body):
            logger.info("WS 认证响应: %s", json.dumps(body, ensure_ascii=False)[:300])
            return
        logger.error(
            "WS 认证被拒绝（凭据可能已失效）: %s",
            json.dumps(body, ensure_ascii=False)[:300],
        )
        refreshed = self._try_refresh_credentials("WS 认证被拒")
        try:
            ws.close()
        except Exception:
            logger.debug("认证失败后关闭 WebSocket 异常", exc_info=True)
        if not refreshed:
            logger.warning("认证失败且未能刷新凭据，将按指数退避重连")

    # -- 认证 --

    def _send_auth(self, ws):
        auth_body = {
            "person": self._person_id,
            "deviceId": self._device_id,
            "signature": self._jwt_token,
            "deviceName": self._device_id,
            "platformName": "web",
            "reconnect": 0,
        }
        payload = {
            "time": str(int(time.time() * 1000)),
            "body": json.dumps(auth_body),
            "event": EVENT_AUTH,
        }
        ws.send(json.dumps(payload))
        logger.info("已发送认证信息")

    # -- 心跳 --

    def _send_heartbeat(self, ws):
        try:
            ws.send(json.dumps({
                "time": str(int(time.time() * 1000)),
                "body": self._hb_body,
                "event": EVENT_HEARTBEAT,
            }))
        except Exception as e:
            logger.debug("发送心跳失败（连接可能已关闭）: %s", e)

    def _heartbeat_loop(self, ws):
        while self._running:
            time.sleep(self.heartbeat_interval)
            if not (ws.sock and ws.sock.connected):
                break
            self._send_heartbeat(ws)
            if self._connection_is_stale():
                logger.warning(
                    "超过 %.0fs 未收到任何服务端数据，判定连接已失效（半开/静默断网），主动断开重连",
                    self.stale_connection_timeout,
                )
                try:
                    ws.close()
                except Exception:
                    logger.debug("关闭失效连接异常", exc_info=True)
                break

    def _connection_is_stale(self) -> bool:
        """收包水位检测：半开 TCP 时发送不报错、recv 永久阻塞，
        只有「多久没收到数据」能可靠暴露这种状态。"""
        if self.stale_connection_timeout <= 0:
            return False
        if not self._last_recv_time:
            return False
        return (time.time() - self._last_recv_time) > self.stale_connection_timeout

    # -- 聊天消息处理 --

    @staticmethod
    def _safe_json_parse(raw, fallback=None):
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return _json_loads(raw)
            except (json.JSONDecodeError, ValueError):
                return fallback if fallback is not None else {}
        return fallback if fallback is not None else {}

    def _handle_chat(self, data: dict):
        # 第一阶段：解析消息体。解析失败才算「解析聊天消息失败」。
        try:
            body = self._safe_json_parse(data.get("body", {}))
            msg_data = self._safe_json_parse(body.get("data", {}))
            if not msg_data:
                return

            # 忽略自己发的消息
            if msg_data.get("person") == self._person_id:
                return

            # 通过名称解析器获取友好名称（batch_resolve_users 为线程池
            # 异步预热，user_cached/area/channel 只读缓存，均不阻塞 WS 线程）
            resolver = get_resolver()
            person_id = msg_data.get("person", "")
            channel_id = msg_data.get("channel", "")
            area_id = msg_data.get("area", "")

            resolver.register_ids(areas=area_id, channels=channel_id, users=person_id)
            if person_id:
                resolver.batch_resolve_users([person_id])
            user_display = resolver.user_cached(person_id)
            area_display = resolver.area(area_id)
            channel_display = resolver.channel(channel_id)

            logger.info(
                f"[聊天] 域={area_display} 频道={channel_display} "
                f"用户={user_display} "
                f"内容={msg_data.get('content', '')[:100]}"
            )
        except Exception as e:
            logger.error(f"解析聊天消息失败: {e}")
            return

        # 第二阶段：业务处理（含回复发送）。与解析分开，避免发送被风控
        # 拦截时误报为「解析失败」；风控拦截已在发送层记录，这里静默放行。
        if not self.on_chat_message:
            return
        try:
            self.on_chat_message(msg_data)
        except SensitiveContentError:
            logger.debug("回复被平台风控拦截，已忽略（发送层已记录）")
        except Exception as e:
            logger.error(f"处理聊天消息失败: {e}")
