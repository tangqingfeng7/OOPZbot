from __future__ import annotations

import time
from collections import deque
from typing import Any, Awaitable, Callable, Mapping

from config import OOPZ_CONFIG
from core.logger_config import get_logger
from oopz.name_resolver import get_resolver
from onebot_v11.message import build_oopz_send_payload, from_v11_message, normalize_v11_message, to_v11_message
from onebot_v11.store import (
    MessageRecord,
    OneBotStore,
    make_group_source,
    make_message_source,
    make_self_source,
    make_user_source,
    parse_group_source,
    parse_message_source,
    parse_user_source,
)

logger = get_logger("OneBotV11")

JsonDict = dict[str, Any]
EventSink = Callable[[JsonDict], Awaitable[None] | None]

EVENT_SERVER_ID = 1
EVENT_FRIEND_REQUEST = 2
EVENT_PRIVATE_MESSAGE_DELETE = 6
EVENT_PRIVATE_MESSAGE = 7
EVENT_MESSAGE_DELETE = 8
EVENT_CHAT_MESSAGE = 9
EVENT_AUTH = 253
EVENT_HEARTBEAT = 254


def ok(data: Any = None, *, echo: Any = None) -> JsonDict:
    payload = {"status": "ok", "retcode": 0, "data": data, "message": ""}
    if echo is not None:
        payload["echo"] = echo
    return payload


def failed(retcode: int, message: str, *, echo: Any = None) -> JsonDict:
    payload = {"status": "failed", "retcode": retcode, "data": None, "message": message}
    if echo is not None:
        payload["echo"] = echo
    return payload


def parse_oopz_timestamp(value: Any) -> int:
    try:
        num = int(str(value or "").strip())
    except ValueError:
        return int(time.time())
    if num > 10_000_000_000_000:
        return int(num / 1_000_000)
    if num > 10_000_000_000:
        return int(num / 1_000)
    return num


def safe_json_parse(raw: Any, fallback: Any = None) -> Any:
    import json
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {} if fallback is None else fallback
    return {} if fallback is None else fallback


class OneBotV11Adapter:
    protocol = "onebot.v11"

    def __init__(
        self,
        sender: Any,
        *,
        self_oopz_id: str = "",
        db_path: str,
        enable_area_scoped_group_ban: bool = False,
        enable_set_group_kick_as_area_kick: bool = False,
        enable_set_group_leave_as_area_leave: bool = False,
    ) -> None:
        self.sender = sender
        self.self_oopz_id = str(self_oopz_id or OOPZ_CONFIG.get("person_uid") or "")
        self.store = OneBotStore(db_path)
        self.self_id = self.store.create_id(make_self_source(self.self_oopz_id)).number
        self._event_sinks: list[EventSink] = []
        self._event_queue: deque[JsonDict] = deque(maxlen=1000)
        self.enable_area_scoped_group_ban = enable_area_scoped_group_ban
        self.enable_set_group_kick_as_area_kick = enable_set_group_kick_as_area_kick
        self.enable_set_group_leave_as_area_leave = enable_set_group_leave_as_area_leave
        self._actions = self._build_actions()

    def _build_actions(self) -> dict[str, Callable[[Mapping[str, Any]], Awaitable[Any]]]:
        actions = {
            "get_supported_actions": self.get_supported_actions,
            ".get_supported_actions": self.get_supported_actions,
            "get_latest_events": self.get_latest_events,
            "get_status": self.get_status,
            "get_version": self.get_version_info,
            "get_version_info": self.get_version_info,
            "can_send_image": self.can_send_image,
            "can_send_record": self.can_send_record,
            "send_msg": self.send_msg,
            "send_group_msg": self.send_group_msg,
            "send_private_msg": self.send_private_msg,
            "delete_msg": self.delete_msg,
            "recall_message": self.delete_msg,
            "get_msg": self.get_msg,
            "get_login_info": self.get_login_info,
            "get_stranger_info": self.get_stranger_info,
            "get_friend_list": self.get_friend_list,
            "set_friend_add_request": self.set_friend_add_request,
            "get_group_list": self.get_group_list,
            "get_group_info": self.get_group_info,
            "get_group_member_info": self.get_group_member_info,
            "get_group_member_list": self.get_group_member_list,
            "set_group_name": self.set_group_name,
            "cleanup_message_mapping": self.cleanup_message_mapping,
        }
        if self.enable_area_scoped_group_ban:
            actions["set_group_ban"] = self.set_group_ban
        if self.enable_set_group_kick_as_area_kick:
            actions["set_group_kick"] = self.set_group_kick
        if self.enable_set_group_leave_as_area_leave:
            actions["set_group_leave"] = self.set_group_leave
        return actions

    def add_event_sink(self, sink: EventSink) -> None:
        self._event_sinks.append(sink)

    def remove_event_sink(self, sink: EventSink) -> None:
        try:
            self._event_sinks.remove(sink)
        except ValueError:
            pass

    async def emit_raw_event(self, raw: dict[str, Any]) -> JsonDict:
        payload = self._to_onebot_event(raw)
        if not payload:
            return {}
        self._event_queue.append(payload)
        for sink in list(self._event_sinks):
            try:
                result = sink(payload)
                if result is not None:
                    await result
            except Exception:
                logger.exception("OneBot v11 事件推送失败")
        return payload

    def _to_onebot_event(self, raw: dict[str, Any]) -> JsonDict:
        try:
            event = int(raw.get("event", -1))
        except (TypeError, ValueError):
            event = -1
        if event in {EVENT_HEARTBEAT, EVENT_SERVER_ID, EVENT_AUTH}:
            return {}
        if event in {EVENT_CHAT_MESSAGE, EVENT_PRIVATE_MESSAGE}:
            return self._message_event(raw, is_private=event == EVENT_PRIVATE_MESSAGE)
        if event in {EVENT_MESSAGE_DELETE, EVENT_PRIVATE_MESSAGE_DELETE}:
            return self._delete_event(raw, is_private=event == EVENT_PRIVATE_MESSAGE_DELETE)
        if event == EVENT_FRIEND_REQUEST:
            return self._friend_request_event(raw)
        return self._meta_event(raw, event)

    def _message_event(self, raw: dict[str, Any], *, is_private: bool) -> JsonDict:
        body = safe_json_parse(raw.get("body"), {})
        msg = safe_json_parse(body.get("data"), {})
        if not isinstance(msg, dict):
            return {}
        if str(msg.get("person") or "") == self.self_oopz_id:
            return {}

        user_id = self.store.create_id(make_user_source(str(msg.get("person") or ""))).number
        message_id = self.store.create_id(
            make_message_source(
                area=str(msg.get("area") or ""),
                channel=str(msg.get("channel") or ""),
                target=str(msg.get("target") or ""),
                message_id=str(msg.get("messageId") or msg.get("id") or ""),
            )
        ).number
        timestamp = parse_oopz_timestamp(msg.get("timestamp"))
        resolver = get_resolver()
        nickname = resolver.user_cached(str(msg.get("person") or ""))

        payload: JsonDict = {
            "time": timestamp,
            "self_id": self.self_id,
            "post_type": "message",
            "message_type": "private" if is_private else "group",
            "sub_type": "friend" if is_private else "normal",
            "message_id": message_id,
            "user_id": user_id,
            "message": to_v11_message(msg, store=self.store),
            "raw_message": str(msg.get("content") or msg.get("text") or ""),
            "font": 0,
            "sender": {"user_id": user_id, "nickname": nickname},
            "extra": {
                "oopz_user_id": str(msg.get("person") or ""),
                "oopz_message_id": str(msg.get("messageId") or msg.get("id") or ""),
            },
        }
        if is_private:
            payload["extra"]["oopz_target_id"] = str(msg.get("target") or msg.get("person") or "")
        else:
            group_id = self.store.create_id(
                make_group_source(area=str(msg.get("area") or ""), channel=str(msg.get("channel") or ""))
            ).number
            payload["group_id"] = group_id
            payload["extra"].update({
                "oopz_area_id": str(msg.get("area") or ""),
                "oopz_channel_id": str(msg.get("channel") or ""),
            })
        self._save_message_mapping(payload)
        return payload

    def _delete_event(self, raw: dict[str, Any], *, is_private: bool) -> JsonDict:
        body = safe_json_parse(raw.get("body"), {})
        data = safe_json_parse(body.get("data"), body)
        person = str(data.get("person") or data.get("target") or "")
        oopz_message_id = str(data.get("messageId") or data.get("message") or data.get("id") or "")
        user_id = self.store.create_id(make_user_source(person)).number if person else 0
        message_id = self.store.create_id(
            make_message_source(
                area=str(data.get("area") or ""),
                channel=str(data.get("channel") or ""),
                target=person if is_private else "",
                message_id=oopz_message_id,
            )
        ).number
        payload: JsonDict = {
            "time": int(time.time()),
            "self_id": self.self_id,
            "post_type": "notice",
            "notice_type": "friend_recall" if is_private else "group_recall",
            "user_id": user_id,
            "message_id": message_id,
            "extra": {
                "oopz_user_id": person,
                "oopz_message_id": oopz_message_id,
            },
        }
        if not is_private:
            area = str(data.get("area") or "")
            channel = str(data.get("channel") or "")
            payload["group_id"] = self.store.create_id(make_group_source(area=area, channel=channel)).number
            payload["operator_id"] = user_id
            payload["extra"].update({"oopz_area_id": area, "oopz_channel_id": channel})
        return payload

    def _friend_request_event(self, raw: dict[str, Any]) -> JsonDict:
        body = safe_json_parse(raw.get("body"), {})
        data = safe_json_parse(body.get("data"), body)
        if not isinstance(data, dict):
            return {}
        person = str(data.get("person") or data.get("uid") or data.get("target") or "")
        request_id = data.get("friendRequestId") or data.get("friend_request_id") or data.get("requestId") or 0
        if not person or not request_id:
            return self._meta_event(raw, EVENT_FRIEND_REQUEST)
        try:
            request_id_int = int(request_id)
        except (TypeError, ValueError):
            return self._meta_event(raw, EVENT_FRIEND_REQUEST)
        user_id = self.store.create_id(make_user_source(person)).number
        return {
            "time": parse_oopz_timestamp(data.get("createTime") or data.get("create_time") or raw.get("time")),
            "self_id": self.self_id,
            "post_type": "request",
            "request_type": "friend",
            "user_id": user_id,
            "comment": str(data.get("name") or data.get("comment") or ""),
            "flag": f"oopz_friend_request:{request_id}:{person}",
            "extra": {
                "oopz_friend_request_id": request_id_int,
                "oopz_user_id": person,
                "oopz_name": str(data.get("name") or ""),
                "oopz_type": str(data.get("type") or ""),
                "oopz_avatar": str(data.get("avatar") or ""),
            },
        }

    def _meta_event(self, raw: dict[str, Any], event: int) -> JsonDict:
        return {
            "time": int(time.time()),
            "self_id": self.self_id,
            "post_type": "meta_event",
            "meta_event_type": "oopz",
            "sub_type": f"event_{event}",
            "oopz_event_type": event,
            "payload": raw,
        }

    async def call_action_payload(self, payload: Mapping[str, Any]) -> JsonDict:
        action = payload.get("action")
        echo = payload.get("echo")
        params = payload.get("params") or {}
        if not isinstance(action, str) or not action:
            return failed(1400, "action must be a non-empty string", echo=echo)
        if not isinstance(params, Mapping):
            return failed(1400, "params must be an object", echo=echo)
        return await self.call_action(action, params, echo=echo)

    async def call_action(self, action: str, params: Mapping[str, Any] | None = None, *, echo: Any = None) -> JsonDict:
        handler = self._actions.get(action)
        if handler is None:
            return failed(1404, f"unsupported action: {action}", echo=echo)
        try:
            data = await handler(params or {})
            return ok(data, echo=echo)
        except ValueError as exc:
            return failed(1400, str(exc), echo=echo)
        except KeyError as exc:
            return failed(1404, str(exc), echo=echo)
        except NotImplementedError as exc:
            return failed(1404, str(exc), echo=echo)
        except Exception as exc:
            logger.exception("OneBot v11 action 执行失败: %s", action)
            return failed(1500, str(exc), echo=echo)

    async def get_supported_actions(self, params: Mapping[str, Any]) -> list[str]:
        return list(self._actions)

    async def get_latest_events(self, params: Mapping[str, Any]) -> list[JsonDict]:
        limit = int(params.get("limit") or 0)
        events = list(self._event_queue)
        return events[-limit:] if limit > 0 else events

    async def get_status(self, params: Mapping[str, Any]) -> JsonDict:
        return {"online": True, "good": True}

    async def get_version_info(self, params: Mapping[str, Any]) -> JsonDict:
        return {"app_name": "oopz-bot", "app_version": "local", "protocol_version": "v11"}

    async def can_send_image(self, params: Mapping[str, Any]) -> JsonDict:
        return {"yes": True}

    async def can_send_record(self, params: Mapping[str, Any]) -> JsonDict:
        return {"yes": False}

    async def cleanup_message_mapping(self, params: Mapping[str, Any]) -> JsonDict:
        seconds = int(params.get("older_than_seconds") or 7 * 24 * 3600)
        return {"deleted": self.store.cleanup_messages(seconds)}

    async def send_msg(self, params: Mapping[str, Any]) -> JsonDict:
        message_type = str(params.get("message_type") or "")
        if message_type == "private":
            return await self.send_private_msg(params)
        if message_type == "group":
            return await self.send_group_msg(params)
        if params.get("group_id") is not None:
            return await self.send_group_msg(params)
        if params.get("user_id") is not None:
            return await self.send_private_msg(params)
        raise ValueError("message_type, group_id or user_id is required")

    async def send_group_msg(self, params: Mapping[str, Any]) -> JsonDict:
        group_id = _require_int(params, "group_id")
        message = params.get("message") or ""
        auto_escape = _parse_bool(params.get("auto_escape"))
        try:
            area, channel = self._resolve_group_id(group_id)
        except ValueError:
            area = str(params.get("oopz_area_id") or params.get("area") or params.get("guild_id") or "")
            channel = str(params.get("oopz_channel_id") or params.get("channel_id") or "")
        if not area or not channel:
            raise ValueError("unknown group_id; provide oopz_area_id and oopz_channel_id")
        self.store.create_id(make_group_source(area=area, channel=channel))
        parts = from_v11_message(message, sender=self.sender, store=self.store, auto_escape=auto_escape)
        text, mention_list, mention_all, attachments = build_oopz_send_payload(parts, store=self.store)
        resp = self.sender.send_message(
            text,
            area=area,
            channel=channel,
            mentionList=mention_list,
            isMentionAll=mention_all,
            attachments=attachments,
        )
        oopz_message_id, timestamp = self._extract_send_result(resp)
        ob_message_id = self.store.create_id(
            make_message_source(area=area, channel=channel, message_id=oopz_message_id)
        ).number
        self._save_sent_mapping(
            ob_message_id,
            oopz_message_id,
            "group",
            area=area,
            channel=channel,
            timestamp=timestamp,
            raw=self._sent_message_raw(
                "group",
                normalize_v11_message(message, auto_escape=auto_escape),
                group_id=group_id,
                timestamp=timestamp,
            ),
        )
        return {"message_id": ob_message_id}

    async def send_private_msg(self, params: Mapping[str, Any]) -> JsonDict:
        user_id = _require_int(params, "user_id")
        message = params.get("message") or ""
        auto_escape = _parse_bool(params.get("auto_escape"))
        target = self._resolve_user_id(user_id)
        parts = from_v11_message(message, sender=self.sender, store=self.store, auto_escape=auto_escape)
        text, mention_list, mention_all, attachments = build_oopz_send_payload(parts, store=self.store)
        result = self.sender.send_private_message(
            target,
            text,
            attachments=attachments,
        )
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(str(result["error"]))
        oopz_message_id = self._extract_message_id_from_payload(result) or str(int(time.time() * 1_000_000))
        channel = str(result.get("channel") or "") if isinstance(result, dict) else ""
        ob_message_id = self.store.create_id(
            make_message_source(target=target, message_id=oopz_message_id)
        ).number
        self._save_sent_mapping(
            ob_message_id,
            oopz_message_id,
            "private",
            channel=channel,
            target=target,
            user_id=target,
            raw=self._sent_message_raw(
                "private",
                normalize_v11_message(message, auto_escape=auto_escape),
                user_id=user_id,
            ),
        )
        return {"message_id": ob_message_id}

    async def delete_msg(self, params: Mapping[str, Any]) -> None:
        message_id = _require_int(params, "message_id")
        record = self.store.get_message(message_id)
        area = channel = target = oopz_message_id = ""
        if record is not None:
            area, channel, target, oopz_message_id = record.area, record.channel, record.target, record.oopz_message_id
        else:
            id_record = self.store.try_resolve_id(message_id)
            if id_record is not None:
                area, channel, target, oopz_message_id = parse_message_source(id_record.source)
        area = str(params.get("oopz_area_id") or params.get("area") or params.get("guild_id") or area or "")
        channel = str(params.get("oopz_channel_id") or params.get("channel_id") or channel or "")
        target = str(params.get("target") or params.get("user_id") or target or "")
        if not oopz_message_id:
            oopz_message_id = str(message_id)
        if record is not None and record.detail_type == "private":
            result = self.sender.recall_private_message(
                oopz_message_id,
                channel=channel,
                target=target or record.user_id,
                area=area or None,
            )
        elif target and not area:
            result = self.sender.recall_private_message(oopz_message_id, channel=channel, target=target, area=None)
        else:
            if not area or not channel:
                raise ValueError("message mapping not found; provide oopz_area_id and oopz_channel_id")
            result = self.sender.recall_message(oopz_message_id, area=area, channel=channel, target=target)
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(str(result["error"]))
        return None

    async def set_friend_add_request(self, params: Mapping[str, Any]) -> JsonDict:
        approve = _require_bool(params, "approve")
        remark = str(params.get("remark") or "")
        flag = str(params.get("flag") or "").strip()
        prefix = "oopz_friend_request:"
        if not flag.startswith(prefix):
            raise ValueError("invalid friend request flag")
        rest = flag.removeprefix(prefix)
        request_id_text, sep, uid = rest.partition(":")
        if not sep or not request_id_text or not uid:
            raise ValueError("invalid friend request flag")
        try:
            request_id = int(request_id_text)
        except ValueError:
            raise ValueError("invalid friend request flag: invalid request id") from None
        result = self.sender.post_friendship_response(uid, request_id, approve)
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(str(result["error"]))
        if approve and remark:
            remark_result = self.sender.set_user_remark_name(uid, remark)
            if isinstance(remark_result, dict) and "error" in remark_result:
                raise RuntimeError(str(remark_result["error"]))
        return {}

    async def get_msg(self, params: Mapping[str, Any]) -> JsonDict:
        message_id = _require_int(params, "message_id")
        record = self.store.get_message(message_id)
        if record is None:
            raise ValueError(f"message mapping not found: {message_id}")
        raw = record.raw or {}
        if raw.get("post_type") == "message":
            data = {
                "time": int(raw.get("time") or record.created_at or int(time.time())),
                "message_type": raw.get("message_type") or record.detail_type,
                "message_id": message_id,
                "real_id": message_id,
                "sender": raw.get("sender") or {},
                "message": raw.get("message") or [],
            }
            if raw.get("group_id") is not None:
                data["group_id"] = raw.get("group_id")
            if raw.get("user_id") is not None:
                data["user_id"] = raw.get("user_id")
            return data
        data = {
            "time": record.created_at or int(time.time()),
            "message_type": "private" if record.detail_type == "private" else "group",
            "message_id": message_id,
            "real_id": message_id,
            "sender": {"user_id": self.self_id, "nickname": ""},
            "message": raw.get("message") or [],
        }
        if record.detail_type == "group" and record.area and record.channel:
            data["group_id"] = self.store.create_id(make_group_source(area=record.area, channel=record.channel)).number
        return data

    async def get_login_info(self, params: Mapping[str, Any]) -> JsonDict:
        detail = self.sender.get_self_detail()
        return {
            "user_id": self.self_id,
            "nickname": str(detail.get("name") or detail.get("nickname") or ""),
            "extra": detail,
        }

    async def get_stranger_info(self, params: Mapping[str, Any]) -> JsonDict:
        user_id = _require_int(params, "user_id")
        uid = self._resolve_user_id(user_id)
        detail = self.sender.get_person_detail_full(uid)
        return {
            "user_id": user_id,
            "nickname": str(detail.get("name") or detail.get("nickname") or ""),
            "sex": "unknown",
            "age": 0,
            "extra": detail,
        }

    async def get_friend_list(self, params: Mapping[str, Any]) -> list[JsonDict]:
        friends = self.sender.get_friendship()
        output: list[JsonDict] = []
        for friend in friends or []:
            if not isinstance(friend, Mapping):
                continue
            uid = str(friend.get("uid") or friend.get("person") or friend.get("id") or "")
            if not uid:
                continue
            user_id = self.store.create_id(make_user_source(uid)).number
            output.append({
                "user_id": user_id,
                "nickname": str(friend.get("name") or friend.get("nickname") or uid),
                "remark": str(friend.get("remark") or friend.get("remarkName") or ""),
                "extra": {"oopz_user_id": uid, **dict(friend)},
            })
        return output

    async def get_group_list(self, params: Mapping[str, Any]) -> list[JsonDict]:
        groups: list[JsonDict] = []
        for area_obj in self.sender.get_joined_areas(quiet=True) or []:
            area = str(area_obj.get("id") or area_obj.get("area") or area_obj.get("area_id") or "")
            if not area:
                continue
            for group in self.sender.get_area_channels(area=area, quiet=True) or []:
                for channel_obj in group.get("channels") or []:
                    channel = str(channel_obj.get("id") or channel_obj.get("channel") or channel_obj.get("channelId") or "")
                    if not channel:
                        continue
                    group_id = self.store.create_id(make_group_source(area=area, channel=channel)).number
                    groups.append({
                        "group_id": group_id,
                        "group_name": str(channel_obj.get("name") or channel),
                        "member_count": 0,
                        "max_member_count": 0,
                        "extra": {"oopz_area_id": area, "oopz_channel_id": channel},
                    })
        return groups

    async def get_group_info(self, params: Mapping[str, Any]) -> JsonDict:
        group_id = _require_int(params, "group_id")
        area, channel = self._resolve_group_id(group_id)
        info = self.sender.get_channel_setting_info(channel)
        return {
            "group_id": group_id,
            "group_name": str(info.get("name") or channel),
            "member_count": 0,
            "max_member_count": 0,
            "extra": {"oopz_area_id": area, "oopz_channel_id": channel},
        }

    async def get_group_member_info(self, params: Mapping[str, Any]) -> JsonDict:
        group_id = _require_int(params, "group_id")
        user_id = _require_int(params, "user_id")
        area, channel = self._resolve_group_id(group_id)
        uid = self._resolve_user_id(user_id)
        info = self.sender.get_person_detail_full(uid)
        area_detail = self.sender.get_user_area_detail(uid, area=area)
        return self._format_member(group_id, user_id, uid, area, channel, info, area_detail)

    async def get_group_member_list(self, params: Mapping[str, Any]) -> list[JsonDict]:
        group_id = _require_int(params, "group_id")
        area, channel = self._resolve_group_id(group_id)
        members = self._get_all_area_members(area)
        uids = [
            str(member.get("uid") or member.get("id") or member.get("person") or "")
            for member in members
            if isinstance(member, Mapping)
        ]
        uids = [uid for uid in uids if uid]
        profile_map = self.sender.get_person_infos_batch(uids) if uids else {}
        output: list[JsonDict] = []
        for member in members:
            if not isinstance(member, Mapping):
                continue
            uid = str(member.get("uid") or member.get("id") or member.get("person") or "")
            if not uid:
                continue
            user_id = self.store.create_id(make_user_source(uid)).number
            info = profile_map.get(uid) if isinstance(profile_map, dict) else None
            if not isinstance(info, Mapping):
                info = member
            output.append(self._format_member(group_id, user_id, uid, area, channel, info, member))
        return output

    async def set_group_name(self, params: Mapping[str, Any]) -> JsonDict:
        group_id = _require_int(params, "group_id")
        name = str(params.get("group_name") or "").strip()
        if not name:
            return {}
        area, channel = self._resolve_group_id(group_id)
        result = self.sender.update_channel(area=area, channel_id=channel, name=name)
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(str(result["error"]))
        return {}

    async def set_group_ban(self, params: Mapping[str, Any]) -> JsonDict:
        group_id = _require_int(params, "group_id")
        user_id = _require_int(params, "user_id")
        duration_seconds = int(params.get("duration") or 0)
        area, _channel = self._resolve_group_id(group_id)
        uid = self._resolve_user_id(user_id)
        if duration_seconds > 0:
            result = self.sender.mute_user(uid, area=area, duration=max(1, int((duration_seconds + 59) / 60)))
        else:
            result = self.sender.unmute_user(uid, area=area)
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(str(result["error"]))
        return {}

    async def set_group_kick(self, params: Mapping[str, Any]) -> JsonDict:
        group_id = _require_int(params, "group_id")
        user_id = _require_int(params, "user_id")
        area, _channel = self._resolve_group_id(group_id)
        uid = self._resolve_user_id(user_id)
        if _parse_bool(params.get("reject_add_request")):
            result = self.sender.block_user_in_area(uid, area=area)
        else:
            result = self.sender.remove_from_area(uid, area=area)
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(str(result["error"]))
        return {}

    async def set_group_leave(self, params: Mapping[str, Any]) -> JsonDict:
        group_id = _require_int(params, "group_id")
        area, _channel = self._resolve_group_id(group_id)
        result = self.sender.leave_area(area)
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(str(result["error"]))
        return {}

    def _get_all_area_members(self, area: str) -> list[Mapping[str, Any]]:
        members: list[Mapping[str, Any]] = []
        page_size = 100
        start = 0
        seen: set[str] = set()
        while True:
            result = self.sender.get_area_members(area=area, offset_start=start, offset_end=start + page_size - 1, quiet=True)
            if not isinstance(result, dict) or result.get("error"):
                break
            batch = result.get("members") or []
            if not isinstance(batch, list) or not batch:
                break
            for member in batch:
                if not isinstance(member, Mapping):
                    continue
                uid = str(member.get("uid") or member.get("id") or member.get("person") or "")
                if uid and uid in seen:
                    continue
                if uid:
                    seen.add(uid)
                members.append(member)
            try:
                total = int(result.get("userCount") or result.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            if total and len(members) >= total:
                break
            if len(batch) < page_size:
                break
            start += page_size
            if start >= 1000:
                break
        return members

    def _resolve_user_id(self, user_id: int | str) -> str:
        record = self.store.try_resolve_id(user_id)
        if record is None:
            raise ValueError(f"unknown user_id: {user_id}")
        return parse_user_source(record.source)

    def _resolve_group_id(self, group_id: int | str) -> tuple[str, str]:
        record = self.store.try_resolve_id(group_id)
        if record is None:
            raise ValueError(f"unknown group_id: {group_id}")
        return parse_group_source(record.source)

    def _save_message_mapping(self, payload: Mapping[str, Any]) -> None:
        extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
        oopz_message_id = str(extra.get("oopz_message_id") or "")
        if not oopz_message_id or not payload.get("message_id"):
            return
        self._save_sent_mapping(
            int(payload["message_id"]),
            oopz_message_id,
            str(payload.get("message_type") or "group"),
            area=str(extra.get("oopz_area_id") or ""),
            channel=str(extra.get("oopz_channel_id") or ""),
            target=str(extra.get("oopz_target_id") or ""),
            user_id=str(extra.get("oopz_user_id") or ""),
            timestamp=str(payload.get("time") or ""),
            raw=dict(payload),
        )

    def _save_sent_mapping(
        self,
        ob_message_id: int | str,
        oopz_message_id: str,
        detail_type: str,
        *,
        area: str = "",
        channel: str = "",
        target: str = "",
        user_id: str = "",
        timestamp: str = "",
        raw: Mapping[str, Any] | None = None,
    ) -> None:
        self.store.save_message(
            MessageRecord(
                ob_message_id=str(ob_message_id),
                oopz_message_id=str(oopz_message_id),
                detail_type=detail_type,
                area=area,
                channel=channel,
                target=target,
                user_id=user_id,
                created_at=parse_oopz_timestamp(timestamp),
                raw=dict(raw or {}),
            )
        )

    def _sent_message_raw(
        self,
        message_type: str,
        message: list[dict[str, Any]],
        *,
        group_id: int | str | None = None,
        user_id: int | str | None = None,
        timestamp: str = "",
    ) -> JsonDict:
        raw_message = "".join(str((segment.get("data") or {}).get("text") or "") for segment in message)
        payload: JsonDict = {
            "time": parse_oopz_timestamp(timestamp),
            "post_type": "message",
            "message_type": message_type,
            "sender": {"user_id": self.self_id, "nickname": ""},
            "message": message,
            "raw_message": raw_message,
        }
        if group_id is not None:
            payload["group_id"] = int(group_id)
        if user_id is not None:
            payload["user_id"] = int(user_id)
        return payload

    def _extract_send_result(self, response: Any) -> tuple[str, str]:
        payload = None
        if hasattr(response, "json"):
            payload = response.json()
        elif isinstance(response, dict):
            payload = response
        message_id = self._extract_message_id_from_payload(payload) or str(int(time.time() * 1_000_000))
        timestamp = self._extract_timestamp_from_payload(payload) or str(int(time.time() * 1_000_000))
        return message_id, timestamp

    @staticmethod
    def _extract_message_id_from_payload(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("messageId", "message_id", "id"):
                if payload.get(key):
                    return str(payload[key])
            for key in ("data", "result", "message"):
                found = OneBotV11Adapter._extract_message_id_from_payload(payload.get(key))
                if found:
                    return found
        return ""

    @staticmethod
    def _extract_timestamp_from_payload(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("timestamp", "time"):
                if payload.get(key):
                    return str(payload[key])
            for key in ("data", "result", "message"):
                found = OneBotV11Adapter._extract_timestamp_from_payload(payload.get(key))
                if found:
                    return found
        return ""

    @staticmethod
    def _format_member(
        group_id: int,
        user_id: int,
        uid: str,
        area: str,
        channel: str,
        info: Mapping[str, Any],
        area_detail: Mapping[str, Any],
    ) -> JsonDict:
        nickname = str(info.get("name") or info.get("nickname") or info.get("displayName") or uid)
        card = str(area_detail.get("nickname") or area_detail.get("name") or "")
        role_name = str(area_detail.get("roleName") or area_detail.get("role") or "")
        roles = area_detail.get("roles")
        if isinstance(roles, list):
            role_names = [str(role.get("name") or role.get("roleName") or role.get("role") or "") for role in roles if isinstance(role, Mapping)]
            role_name = " ".join(role_names) or role_name
        role_text = "owner" if "域主" in role_name else "member"
        shut_up = area_detail.get("disableTextTo") or area_detail.get("disable_text_to") or area_detail.get("shut_up_timestamp") or 0
        try:
            shut_up_timestamp = int(int(shut_up) / 1000) if int(shut_up) > 10_000_000_000 else int(shut_up)
        except (TypeError, ValueError):
            shut_up_timestamp = 0
        return {
            "group_id": group_id,
            "user_id": user_id,
            "nickname": nickname,
            "card": card,
            "sex": "unknown",
            "age": 0,
            "area": "",
            "join_time": 0,
            "last_sent_time": 0,
            "level": str(info.get("memberLevel") or info.get("level") or ""),
            "role": role_text,
            "unfriendly": False,
            "title": "",
            "title_expire_time": 0,
            "card_changeable": False,
            "shut_up_timestamp": shut_up_timestamp,
            "extra": {"oopz_area_id": area, "oopz_channel_id": channel, "oopz_user_id": uid},
        }


def _require_int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if value is None or value == "":
        raise ValueError(f"{key} is required")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer") from None


def _require_bool(data: Mapping[str, Any], key: str) -> bool:
    if key not in data:
        raise ValueError(f"{key} is required")
    return _parse_bool(data.get(key))


def _parse_bool(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}
