"""从 Bot 私信中捕获 Oopz 域邀请并写入待审批收件箱。"""

from __future__ import annotations

import json
import re
from typing import Any

from core.database import AreaInviteRequestDB
from core.logger_config import get_logger
from oopz.name_resolver import get_resolver

logger = get_logger("AreaInviteInbox")

_INVITE_URL_RE = re.compile(
    r"https?://(?:www\.)?oopz\.(?:cn|vip)/(?:i|s)/([A-Za-z0-9_-]{4,64})",
    re.IGNORECASE,
)
_INVITE_CODE_RE = re.compile(r"[A-Za-z0-9_-]{4,64}")


def extract_area_invite_codes(value: object) -> list[str]:
    """从任意文本中提取所有 Oopz 邀请链接短码并去重。"""
    text = str(value or "")
    return list(dict.fromkeys(match.group(1) for match in _INVITE_URL_RE.finditer(text)))


def normalize_area_invite_code(value: object) -> str:
    """接受可信存储中的短码，也兼容完整 Oopz 邀请链接。"""
    text = str(value or "").strip()
    codes = extract_area_invite_codes(text)
    if codes:
        return codes[0]
    if _INVITE_CODE_RE.fullmatch(text):
        return text
    raise ValueError("无效的 Oopz 域邀请码")


def _message_search_text(message: dict[str, Any]) -> str:
    parts = [str(message.get("content") or ""), str(message.get("text") or "")]
    for key in ("cards", "referenceMessage"):
        value = message.get(key)
        if value is None:
            continue
        try:
            parts.append(json.dumps(value, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            parts.append(str(value))
    return "\n".join(parts)


async def _sender_name(sender_id: str) -> str:
    if not sender_id:
        return ""
    resolver = get_resolver()
    try:
        names = await resolver.ensure_users([sender_id])
        return str(names.get(sender_id) or resolver.user_cached(sender_id))
    except Exception:
        logger.debug("邀请发送者名称解析失败: %s", sender_id, exc_info=True)
        return resolver.user_cached(sender_id)


async def capture_private_area_invites(sender: Any, message: dict[str, Any]) -> int:
    """识别一条私信中的域邀请；只记录，不自动加入域。"""
    codes = extract_area_invite_codes(_message_search_text(message))
    if not codes:
        return 0

    sender_id = str(message.get("person") or "").strip()
    sender_name = await _sender_name(sender_id)
    captured = 0
    for code in codes:
        try:
            detail = await sender.get_area_invite_detail(code)
            if not isinstance(detail, dict) or detail.get("error"):
                raise RuntimeError(
                    str(detail.get("error") or "邀请信息返回格式异常")
                    if isinstance(detail, dict)
                    else "邀请信息返回格式异常"
                )
            if not bool(detail.get("isAreaInvite")) or not str(detail.get("area") or "").strip():
                continue
            await AreaInviteRequestDB.upsert_pending(
                code=code,
                sender_id=sender_id,
                sender_name=sender_name,
                message_id=str(message.get("messageId") or ""),
                message_timestamp=str(message.get("timestamp") or ""),
                detail=detail,
            )
            captured += 1
        except Exception:
            logger.warning("私信域邀请识别失败: code=%s sender=%s", code, sender_id, exc_info=True)

    if captured:
        logger.info("已记录 %d 条私信域邀请，等待后台管理员审批", captured)
    return captured


__all__ = [
    "capture_private_area_invites",
    "extract_area_invite_codes",
    "normalize_area_invite_code",
]
