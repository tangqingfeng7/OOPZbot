from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from core.constants import build_mention
from core.logger_config import get_logger
from onebot_v11.store import OneBotStore, make_message_source, make_user_source, parse_message_source

logger = get_logger("OneBotV11Message")

_CQ_RE = re.compile(r"\[CQ:(?P<type>[a-zA-Z0-9_]+)(?P<params>[^\]]*)\]")
_MENTION_RE = re.compile(r"\(met\)(?P<uid>[^()]+)\(met\)")


@dataclass
class SendParts:
    text_parts: list[str] = field(default_factory=list)
    mention_ids: list[str] = field(default_factory=list)
    mention_all: bool = False
    attachments: list[dict[str, Any]] = field(default_factory=list)
    reference_message_id: str = ""


def _parse_cq_params(raw: str) -> dict[str, str]:
    raw = raw.lstrip(",")
    result: dict[str, str] = {}
    for item in raw.split(","):
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        result[key] = value.replace("&#44;", ",").replace("&amp;", "&").replace("&#91;", "[").replace("&#93;", "]")
    return result


def from_v11_message(
    message: Any,
    *,
    sender: Any = None,
    store: OneBotStore | None = None,
    auto_escape: bool = False,
) -> SendParts:
    parts = SendParts()
    if auto_escape and isinstance(message, str):
        parts.text_parts.append(message)
        return parts

    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict):
                parts.text_parts.append(str(segment))
                continue
            _append_segment(parts, segment.get("type"), segment.get("data") or {}, sender=sender, store=store)
        return parts

    text = str(message or "")
    pos = 0
    for match in _CQ_RE.finditer(text):
        if match.start() > pos:
            parts.text_parts.append(text[pos:match.start()])
        _append_segment(parts, match.group("type"), _parse_cq_params(match.group("params")), sender=sender, store=store)
        pos = match.end()
    if pos < len(text):
        parts.text_parts.append(text[pos:])
    return parts


def normalize_v11_message(message: Any, *, auto_escape: bool = False) -> list[dict[str, Any]]:
    if auto_escape and isinstance(message, str):
        return [{"type": "text", "data": {"text": message}}]

    if isinstance(message, list):
        segments: list[dict[str, Any]] = []
        for segment in message:
            if isinstance(segment, dict):
                segments.append({
                    "type": str(segment.get("type") or "text"),
                    "data": dict(segment.get("data") or {}),
                })
            else:
                segments.append({"type": "text", "data": {"text": str(segment)}})
        return segments

    text = str(message or "")
    segments: list[dict[str, Any]] = []
    pos = 0
    for match in _CQ_RE.finditer(text):
        if match.start() > pos:
            segments.append({"type": "text", "data": {"text": text[pos:match.start()]}})
        segments.append({
            "type": match.group("type"),
            "data": _parse_cq_params(match.group("params")),
        })
        pos = match.end()
    if pos < len(text) or not segments:
        segments.append({"type": "text", "data": {"text": text[pos:]}})
    return segments


def _segment_text(parts: SendParts, data: dict[str, Any], *, sender: Any, store: OneBotStore | None) -> None:
    parts.text_parts.append(str(data.get("text") or ""))


def _segment_at(parts: SendParts, data: dict[str, Any], *, sender: Any, store: OneBotStore | None) -> None:
    qq = str(data.get("qq") or data.get("user_id") or "")
    if qq == "all":
        parts.mention_all = True
    elif qq:
        parts.mention_ids.append(qq)


def _segment_image(parts: SendParts, data: dict[str, Any], *, sender: Any, store: OneBotStore | None) -> None:
    attachment = _image_attachment(data, sender=sender)
    if not attachment:
        return
    parts.attachments.append(attachment)
    file_key = attachment.get("fileKey") or attachment.get("file") or ""
    if file_key:
        parts.text_parts.append(f"![IMAGE]({file_key})")


def _segment_reply(parts: SendParts, data: dict[str, Any], *, sender: Any, store: OneBotStore | None) -> None:
    reference = _resolve_reference_oopz_id(data, store)
    if reference:
        parts.reference_message_id = reference


# Register a handler here to support a new outbound message segment type.
_SEGMENT_HANDLERS: dict[str, Callable[..., None]] = {
    "text": _segment_text,
    "at": _segment_at,
    "image": _segment_image,
    "reply": _segment_reply,
}


def _append_segment(
    parts: SendParts,
    seg_type: Any,
    data: dict[str, Any],
    *,
    sender: Any,
    store: OneBotStore | None,
) -> None:
    handler = _SEGMENT_HANDLERS.get(str(seg_type or ""))
    if handler is None:
        parts.text_parts.append(f"[{seg_type}:{data}]")
        return
    handler(parts, data, sender=sender, store=store)


def _resolve_reference_oopz_id(data: dict[str, Any], store: OneBotStore | None) -> str:
    if store is None:
        return ""
    ref = str(data.get("id") or "").strip()
    if not ref:
        return ""
    record = store.get_message(ref)
    if record is not None and record.oopz_message_id:
        return record.oopz_message_id
    id_record = store.try_resolve_id(ref)
    if id_record is None:
        return ""
    try:
        _area, _channel, _target, message_id = parse_message_source(id_record.source)
    except ValueError:
        return ""
    return message_id


def _image_attachment(data: dict[str, Any], *, sender: Any) -> dict[str, Any] | None:
    file_ref = str(data.get("file") or data.get("file_id") or data.get("url") or "").strip()
    if not file_ref:
        return None

    if file_ref.startswith(("http://", "https://")) and sender is not None:
        upload_result = sender.upload_file_from_url(file_ref)
        if upload_result.get("code") == "success" and isinstance(upload_result.get("data"), dict):
            return upload_result["data"]
        logger.warning("OneBot v11 图片 URL 上传失败，已跳过该图片段: %s", file_ref)
        return None
    if file_ref.startswith("file://") and sender is not None:
        local_path = file_ref.removeprefix("file://")
        upload = sender.upload_file(local_path, file_type="IMAGE", ext=os.path.splitext(local_path)[1] or ".webp")
        if not upload.get("fileKey"):
            logger.warning("OneBot v11 本地图片上传失败，已跳过该图片段: %s", local_path)
            return None
        return {
            "fileKey": upload.get("fileKey", ""),
            "url": upload.get("url", ""),
            "fileSize": os.path.getsize(local_path) if os.path.exists(local_path) else 0,
            "attachmentType": "IMAGE",
        }

    return {
        "fileKey": file_ref,
        "url": str(data.get("url") or ""),
        "width": int(data.get("width") or 0),
        "height": int(data.get("height") or 0),
        "fileSize": int(data.get("file_size") or data.get("fileSize") or 0),
        "hash": str(data.get("hash") or ""),
        "animated": False,
        "displayName": "",
        "attachmentType": "IMAGE",
    }


def to_v11_message(msg: dict[str, Any], *, store: OneBotStore) -> list[dict[str, Any]]:
    content = str(msg.get("content") or msg.get("text") or "")
    segments: list[dict[str, Any]] = []
    reference = str(msg.get("referenceMessageId") or "").strip()
    if reference:
        reference_id = store.create_id(
            make_message_source(
                area=str(msg.get("area") or ""),
                channel=str(msg.get("channel") or ""),
                message_id=reference,
            )
        ).number
        segments.append({"type": "reply", "data": {"id": str(reference_id)}})
    pos = 0
    for match in _MENTION_RE.finditer(content):
        if match.start() > pos:
            segments.append({"type": "text", "data": {"text": content[pos:match.start()]}})
        uid = match.group("uid")
        user_id = store.create_id(make_user_source(uid)).number
        segments.append({"type": "at", "data": {"qq": user_id}})
        pos = match.end()
    if pos < len(content) or not segments:
        segments.append({"type": "text", "data": {"text": content[pos:]}})

    for attachment in msg.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        if str(attachment.get("attachmentType") or "").upper() != "IMAGE":
            continue
        segments.append({
            "type": "image",
            "data": {
                "file": attachment.get("fileKey") or attachment.get("url") or "",
                "url": attachment.get("url") or "",
            },
        })
    return segments


def _cq_escape(value: str, *, comma: bool) -> str:
    text = value.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")
    return text.replace(",", "&#44;") if comma else text


def cq_from_segments(segments: list[dict[str, Any]]) -> str:
    """Render v11 message segments back into a CQ-code string for ``raw_message``.

    Generic by design: any segment type renders as ``[CQ:type,k=v,...]`` so new
    segment kinds round-trip without touching this function.
    """
    parts: list[str] = []
    for segment in segments:
        seg_type = str(segment.get("type") or "text")
        data = segment.get("data") or {}
        if seg_type == "text":
            parts.append(_cq_escape(str(data.get("text") or ""), comma=False))
            continue
        if not data:
            parts.append(f"[CQ:{seg_type}]")
            continue
        encoded = ",".join(f"{key}={_cq_escape(str(value), comma=True)}" for key, value in data.items())
        parts.append(f"[CQ:{seg_type},{encoded}]")
    return "".join(parts)


def build_oopz_send_payload(
    parts: SendParts, *, store: OneBotStore
) -> tuple[str, list[dict[str, Any]], bool, list[dict[str, Any]], str]:
    text_parts = list(parts.text_parts)
    mention_list: list[dict[str, Any]] = []
    for raw_id in parts.mention_ids:
        uid = str(raw_id)
        record = store.try_resolve_id(raw_id)
        if record is not None:
            uid = record.source.removeprefix("user:")
        mention_list.append({"person": uid, "isBot": False, "botType": "", "offset": -1})
        text_parts.append(build_mention(uid))
    return "".join(text_parts), mention_list, parts.mention_all, parts.attachments, parts.reference_message_id
