from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from aiohttp import ClientConnectorError, ClientSession, WSMsgType, WSServerHandshakeError, web

from core.json_utils import compact_json
from core.logger_config import get_logger
from onebot_v11.config import OneBotV11ServerConfig

logger = get_logger("OneBotV11Server")

JsonDict = dict[str, Any]
WsRole = Literal["api", "event", "universal"]


class _TextWebSocket(Protocol):
    async def send_str(self, data: str, compress: int | None = None) -> None:
        ...


class OneBotV11Server:
    def __init__(self, adapter, config: OneBotV11ServerConfig) -> None:
        self.adapter = adapter
        self.config = config
        self.app = web.Application()
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._session: ClientSession | None = None
        self._ws_clients: dict[web.WebSocketResponse, WsRole] = {}
        self._reverse_tasks: list[asyncio.Task[None]] = []
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._started = False
        self._setup_routes()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.adapter.add_event_sink(self.broadcast_event)
        self._session = ClientSession()
        if self.config.enable_http or self.config.enable_ws:
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            self.site = web.TCPSite(self.runner, host=self.config.host, port=self.config.port)
            await self.site.start()
            logger.info("OneBot v11 服务已启动: http://%s:%s", self.config.host, self.bound_port)
        if self.config.enable_ws_reverse:
            for url, role in self._reverse_targets():
                self._reverse_tasks.append(asyncio.create_task(self._reverse_ws_loop(url, role)))
        if self.config.heartbeat_enabled and self.config.heartbeat_interval > 0:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self.adapter.remove_event_sink(self.broadcast_event)
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        for task in self._reverse_tasks:
            task.cancel()
        if self._reverse_tasks:
            await asyncio.gather(*self._reverse_tasks, return_exceptions=True)
        self._reverse_tasks.clear()
        for ws in list(self._ws_clients):
            await ws.close()
        self._ws_clients.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            self.site = None
        logger.info("OneBot v11 服务已停止")

    @property
    def bound_port(self) -> int:
        addresses = self.runner.addresses if self.runner is not None else []
        for address in addresses:
            if isinstance(address, tuple) and len(address) > 1:
                port = address[1]
                if isinstance(port, int):
                    return port
        return self.config.port

    def _setup_routes(self) -> None:
        if self.config.enable_ws:
            self.app.router.add_get("/", self._handle_ws_universal)
            self.app.router.add_get("/api", self._handle_ws_api)
            self.app.router.add_get("/api/", self._handle_ws_api)
            self.app.router.add_get("/event", self._handle_ws_event)
            self.app.router.add_get("/event/", self._handle_ws_event)
        if self.config.enable_http:
            self.app.router.add_get("/{action}", self._handle_http_action)
            self.app.router.add_post("/{action}", self._handle_http_action)
            self.app.router.add_get("/{action}/", self._handle_http_action)
            self.app.router.add_post("/{action}/", self._handle_http_action)

    async def _handle_http_action(self, request: web.Request) -> web.Response:
        auth_status = self._auth_status(request)
        if auth_status != 200:
            return web.Response(status=auth_status)
        params, error_status = await self._read_http_params(request)
        if error_status is not None:
            return web.Response(status=error_status)
        action = request.match_info.get("action", "")
        response = await self.adapter.call_action(action, params)
        status = 404 if int(response.get("retcode", 0) or 0) == 1404 else 200
        return self._json_response(response, status=status)

    async def _handle_ws_api(self, request: web.Request) -> web.StreamResponse:
        return await self._handle_ws(request, "api")

    async def _handle_ws_event(self, request: web.Request) -> web.StreamResponse:
        return await self._handle_ws(request, "event")

    async def _handle_ws_universal(self, request: web.Request) -> web.StreamResponse:
        return await self._handle_ws(request, "universal")

    async def _handle_ws(self, request: web.Request, role: WsRole) -> web.StreamResponse:
        auth_status = self._auth_status(request)
        if auth_status != 200:
            return web.Response(status=auth_status)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._ws_clients[ws] = role
        try:
            if self.config.send_connect_event and role in {"event", "universal"}:
                await self._ws_send_json(ws, self._connect_event())
            async for msg in ws:
                if role == "event":
                    continue
                if msg.type == WSMsgType.TEXT:
                    await self._ws_send_json(ws, await self._handle_ws_payload_text(msg.data))
                elif msg.type == WSMsgType.BINARY:
                    try:
                        text = msg.data.decode("utf-8")
                    except UnicodeDecodeError:
                        await self._ws_send_json(ws, self._failed(1400, "binary payload must be utf-8 json"))
                        continue
                    await self._ws_send_json(ws, await self._handle_ws_payload_text(text))
        finally:
            self._ws_clients.pop(ws, None)
        return ws

    async def broadcast_event(self, event: JsonDict) -> None:
        await self._broadcast_http_post(event)
        await self._broadcast_forward_ws(event)

    async def _broadcast_forward_ws(self, event: JsonDict) -> None:
        closed: list[web.WebSocketResponse] = []
        for ws, role in list(self._ws_clients.items()):
            if role == "api":
                continue
            if ws.closed:
                closed.append(ws)
                continue
            try:
                await self._ws_send_json(ws, event)
            except Exception:
                logger.exception("OneBot v11 WebSocket 事件发送失败")
                closed.append(ws)
        for ws in closed:
            self._ws_clients.pop(ws, None)

    async def _broadcast_http_post(self, event: JsonDict) -> None:
        if not self.config.enable_http_post or self._session is None or not self.config.http_post_urls:
            return
        raw = compact_json(event).encode("utf-8")
        headers = self._http_post_headers(raw)
        timeout = self.config.http_post_timeout or None
        for url in self.config.http_post_urls:
            try:
                async with self._session.post(url, data=raw, headers=headers, timeout=timeout) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        logger.warning("OneBot v11 HTTP POST 上报失败: %s %s %s", url, resp.status, text[:300])
            except Exception:
                logger.exception("OneBot v11 HTTP POST 上报异常: %s", url)

    async def _handle_ws_payload_text(self, text: str) -> JsonDict:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return self._failed(1400, "invalid json")
        if not isinstance(payload, Mapping):
            return self._failed(1400, "payload must be object")
        return await self.adapter.call_action_payload(payload)

    async def _read_http_params(self, request: web.Request) -> tuple[JsonDict, int | None]:
        if request.method == "GET":
            return {k: v for k, v in request.query.items() if k != "access_token"}, None
        if not request.can_read_body:
            return {}, None
        if request.content_type.lower() == "application/json":
            try:
                data = await request.json()
            except Exception:
                return {}, 400
            if data is None:
                return {}, None
            if not isinstance(data, Mapping):
                return {}, 400
            return dict(data), None
        if request.content_type.lower() == "application/x-www-form-urlencoded":
            return dict(await request.post()), None
        body = await request.read()
        if not body.strip():
            return {}, None
        return {}, 406

    def _auth_status(self, request: web.Request) -> int:
        token = self.config.access_token
        if not token:
            return 200
        auth = request.headers.get("Authorization", "")
        query_token = request.query.get("access_token", "")
        bearer = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if (bearer and hmac.compare_digest(bearer, token)) or (
            query_token and hmac.compare_digest(query_token, token)
        ):
            return 200
        return 403 if auth or query_token else 401

    def _http_post_headers(self, raw_body: bytes) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "X-Self-ID": str(self.adapter.self_id)}
        if self.config.secret:
            digest = hmac.new(self.config.secret.encode("utf-8"), raw_body, hashlib.sha1).hexdigest()
            headers["X-Signature"] = f"sha1={digest}"
        return headers

    def _reverse_targets(self) -> list[tuple[str, WsRole]]:
        targets: list[tuple[str, WsRole]] = []
        if self.config.ws_reverse_url:
            targets.append((self.config.ws_reverse_url, "universal"))
        if self.config.ws_reverse_api_url:
            targets.append((self.config.ws_reverse_api_url, "api"))
        if self.config.ws_reverse_event_url:
            targets.append((self.config.ws_reverse_event_url, "event"))
        seen: set[tuple[str, WsRole]] = set()
        output: list[tuple[str, WsRole]] = []
        for item in targets:
            if item not in seen:
                output.append(item)
                seen.add(item)
        return output

    async def _reverse_ws_loop(self, url: str, role: WsRole) -> None:
        while self._started:
            try:
                await self._connect_reverse_ws(url, role)
            except asyncio.CancelledError:
                raise
            except (ClientConnectorError, WSServerHandshakeError):
                logger.warning("OneBot v11 反向 WebSocket 暂不可用: %s", url)
            except Exception:
                logger.exception("OneBot v11 反向 WebSocket 异常: %s", url)
            if self._started:
                await asyncio.sleep(self.config.ws_reverse_reconnect_interval)

    async def _connect_reverse_ws(self, url: str, role: WsRole) -> None:
        if self._session is None:
            raise RuntimeError("ClientSession is not initialized")
        async with self._session.ws_connect(url, headers=self._reverse_ws_headers(role), heartbeat=30) as ws:
            async def reverse_sink(event: JsonDict) -> None:
                if role in {"event", "universal"}:
                    await self._ws_send_json(ws, event)

            self.adapter.add_event_sink(reverse_sink)
            try:
                if self.config.send_connect_event and role in {"event", "universal"}:
                    await self._ws_send_json(ws, self._connect_event())
                async for msg in ws:
                    if role == "event":
                        continue
                    if msg.type == WSMsgType.TEXT:
                        await self._ws_send_json(ws, await self._handle_ws_payload_text(msg.data))
            finally:
                self.adapter.remove_event_sink(reverse_sink)

    def _reverse_ws_headers(self, role: WsRole) -> dict[str, str]:
        headers = {
            "X-Self-ID": str(self.adapter.self_id),
            "X-Client-Role": {"api": "API", "event": "Event", "universal": "Universal"}[role],
            "User-Agent": "CQHttp/4.15.0",
        }
        if self.config.access_token:
            headers["Authorization"] = f"Bearer {self.config.access_token}"
        return headers

    def _connect_event(self) -> JsonDict:
        return {
            "time": int(time.time()),
            "self_id": self.adapter.self_id,
            "post_type": "meta_event",
            "meta_event_type": "lifecycle",
            "sub_type": "connect",
        }

    async def _heartbeat_loop(self) -> None:
        interval = self.config.heartbeat_interval
        while self._started:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            if not self._started:
                break
            try:
                await self.adapter.emit_event(self._heartbeat_event())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("OneBot v11 心跳广播失败")

    def _heartbeat_event(self) -> JsonDict:
        return {
            "time": int(time.time()),
            "self_id": self.adapter.self_id,
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "status": self.adapter.status_snapshot(),
            "interval": int(self.config.heartbeat_interval * 1000),
        }

    @staticmethod
    def _failed(retcode: int, message: str) -> JsonDict:
        return {"status": "failed", "retcode": retcode, "data": None, "message": message}

    @staticmethod
    def _json_response(data: Any, *, status: int = 200) -> web.Response:
        return web.Response(text=json.dumps(data, ensure_ascii=False), status=status, content_type="application/json")

    @staticmethod
    async def _ws_send_json(ws: _TextWebSocket, data: Any) -> None:
        await ws.send_str(json.dumps(data, ensure_ascii=False))
