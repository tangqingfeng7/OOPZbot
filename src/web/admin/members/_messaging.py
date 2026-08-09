"""向频道发送普通消息 / 公告样式消息。"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from web.admin.shared import (
    _require_sender,
    _resolve_area,
    read_json_body,
    require_sender,
)

router = APIRouter()


@router.post("/admin/api/send-message")
@require_sender
async def admin_send_message(request: Request):
    """发送普通消息到指定频道。"""
    sender = _require_sender()
    body = await read_json_body(request)
    area = (body.get("area") or "").strip() or _resolve_area()
    channel = (body.get("channel") or "").strip()
    text = (body.get("text") or "").strip()

    if not area:
        return JSONResponse({"ok": False, "error": "未指定域"})
    if not channel:
        return JSONResponse({"ok": False, "error": "未指定频道"})
    if not text:
        return JSONResponse({"ok": False, "error": "消息内容不能为空"})

    try:
        resp = sender.send_message(text, area=area, channel=channel, auto_recall=False, styleTags=[])
        result = resp.json()
        if not result.get("status") and result.get("code") not in (0, "0", 200, "200", "success"):
            return JSONResponse({"ok": False, "error": result.get("message") or "发送失败"})
        return JSONResponse({"ok": True, "message": "消息已发送"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/admin/api/send-announcement")
@require_sender
async def admin_send_announcement(request: Request):
    """发送公告样式消息到指定频道。"""
    sender = _require_sender()
    body = await read_json_body(request)
    area = (body.get("area") or "").strip() or _resolve_area()
    channel = (body.get("channel") or "").strip()
    text = (body.get("text") or "").strip()

    if not area:
        return JSONResponse({"ok": False, "error": "未指定域"})
    if not channel:
        return JSONResponse({"ok": False, "error": "未指定频道"})
    if not text:
        return JSONResponse({"ok": False, "error": "公告内容不能为空"})

    try:
        resp = sender.send_message(text, area=area, channel=channel, auto_recall=False, styleTags=["IMPORTANT"])
        result = resp.json()
        if not result.get("status") and result.get("code") not in (0, "0", 200, "200", "success"):
            return JSONResponse({"ok": False, "error": result.get("message") or "发送失败"})
        return JSONResponse({"ok": True, "message": "公告已发送"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
