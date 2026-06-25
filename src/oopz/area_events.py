"""Shared parsing for Oopz area member enter/leave WebSocket events.

These helpers are used both by the area-join notifier (welcome/leave messages)
and by the OneBot v11 adapter (group_increase / group_decrease notices), so the
event taxonomy lives in one place.
"""

from __future__ import annotations

import json
from typing import Optional, Tuple

EVENT_AREA_MEMBER_ENTER = 10
EVENT_AREA_MEMBER_LEAVE = 11

# Oopz pushes member changes under several event codes; these are treated as joins.
OOPZ_JOIN_EVENTS = (17, 18, 20, 21, 22)

_JOIN_KEYS = ("enter", "join", "add", "member_join", "join_area", "subscribe", "1", "enter_area")
_LEAVE_KEYS = ("leave", "exit", "remove", "quit", "member_leave", "leave_area", "unsubscribe", "0")


def parse_member_event(event: int, data: dict) -> Optional[Tuple[str, str, str]]:
    """Classify a raw Oopz event as an area member change.

    Returns ``("join"|"leave", area, uid)`` for area-level member changes, or
    ``None`` when the event is not a recognizable member enter/leave (including
    channel-scoped events, which are ignored here).
    """
    body_raw = data.get("body")
    if body_raw is None:
        return None
    if isinstance(body_raw, str):
        try:
            body = json.loads(body_raw)
        except json.JSONDecodeError:
            body = {}
    else:
        body = body_raw

    inner = body.get("data")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except json.JSONDecodeError:
            inner = {}
    if not inner and isinstance(body.get("data"), dict):
        inner = body["data"]
    if not inner:
        inner = body

    def _str_uid(v) -> str:
        if v is None:
            return ""
        if isinstance(v, dict):
            return str(v.get("id") or v.get("uid") or v.get("personId") or v.get("userId") or "").strip()
        return str(v).strip()

    top = data if isinstance(data, dict) else {}
    area = (inner.get("area") or inner.get("areaId") or body.get("area") or body.get("areaId") or top.get("area") or top.get("areaId") or "").strip()
    if not area and isinstance(inner.get("area"), dict):
        area = str(inner["area"].get("id") or inner["area"].get("areaId") or "").strip()
    uid = _str_uid(inner.get("person")) or _str_uid(inner.get("uid")) or _str_uid(inner.get("target")) or inner.get("userId") or _str_uid(body.get("person")) or body.get("uid") or body.get("userId") or _str_uid(top.get("person")) or top.get("uid") or ""
    if not uid:
        persons = body.get("persons") or inner.get("persons") or []
        if isinstance(persons, list) and persons:
            uid = _str_uid(persons[0]) if isinstance(persons[0], dict) else str(persons[0]).strip()
        else:
            uid = ""

    action_raw = (inner.get("action") or inner.get("type") or inner.get("event") or inner.get("actionType") or body.get("action") or body.get("type") or body.get("actionType") or top.get("action") or top.get("type") or "").strip().lower()

    if not area or not uid:
        return None

    channel_id = (inner.get("channel") or inner.get("channelId") or body.get("channel") or body.get("channelId") or top.get("channel") or top.get("channelId") or "")
    if isinstance(channel_id, dict):
        channel_id = str(channel_id.get("id") or channel_id.get("channelId") or "").strip()
    else:
        channel_id = str(channel_id).strip() if channel_id else ""
    if channel_id:
        return None

    active_num = body.get("activeNum") if isinstance(body.get("activeNum"), (int, float)) else None
    if active_num is None:
        active_num = inner.get("activeNum")

    event_str = str(event).lower() if event is not None else ""
    if event == 19:
        if active_num is not None and active_num != 0:
            return ("join", area, uid)
        return ("leave", area, uid)
    is_join = (
        event == EVENT_AREA_MEMBER_ENTER
        or event in OOPZ_JOIN_EVENTS
        or event_str in ("10", "17", "18", "20", "21", "22", "enter", "join", "area_member_enter", "member_enter")
        or action_raw in _JOIN_KEYS
    )
    is_leave = (
        event == EVENT_AREA_MEMBER_LEAVE
        or event_str in ("11", "leave", "exit", "area_member_leave", "member_leave")
        or action_raw in _LEAVE_KEYS
    )
    if is_join:
        return ("join", area, uid)
    if is_leave:
        return ("leave", area, uid)

    return None
