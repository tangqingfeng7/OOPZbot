from fastapi import APIRouter

from web.admin.shared import (
    JSONResponse,
    MessageStatsDB,
    Query,
    ReminderDB,
    Request,
    ScheduledMessageDB,
    get_resolver,
    get_scheduled_template,
    list_scheduled_templates,
)

router = APIRouter()

# ---------------------------------------------------------------------------
# 定时消息 CRUD API
# ---------------------------------------------------------------------------

@router.get("/admin/api/scheduled-messages")
def admin_scheduled_messages_list():
    return JSONResponse({"ok": True, "items": ScheduledMessageDB.get_all()})


@router.get("/admin/api/scheduled-message-templates")
def admin_scheduled_message_templates():
    return JSONResponse({"ok": True, "items": list_scheduled_templates()})


@router.post("/admin/api/scheduled-message-templates/{template_key}/apply")
async def admin_scheduled_message_template_apply(template_key: str, request: Request):
    template = get_scheduled_template(template_key)
    if not template:
        return JSONResponse({"ok": False, "error": "未找到定时模板"}, status_code=404)
    body = await request.json()
    channel_id = str(body.get("channel_id") or "").strip()
    area_id = str(body.get("area_id") or "").strip()
    if not channel_id or not area_id:
        return JSONResponse({"ok": False, "error": "channel_id/area_id 不能为空"}, status_code=400)
    name = str(body.get("name") or template["name"]).strip()
    message_text = str(body.get("message_text") or template["message_text"]).strip()
    weekdays = str(body.get("weekdays") or template["weekdays"]).strip()
    try:
        cron_hour = int(body.get("cron_hour", template["cron_hour"]))
        cron_minute = int(body.get("cron_minute", template["cron_minute"]))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "cron_hour/cron_minute 必须为整数"}, status_code=400)
    if not name or not message_text:
        return JSONResponse({"ok": False, "error": "name/message_text 不能为空"}, status_code=400)
    task_id = ScheduledMessageDB.create(
        name=name,
        cron_hour=cron_hour,
        cron_minute=cron_minute,
        channel_id=channel_id,
        area_id=area_id,
        message_text=message_text,
        weekdays=weekdays,
    )
    return JSONResponse({"ok": True, "id": task_id, "template": template["key"]})


@router.post("/admin/api/scheduled-messages")
async def admin_scheduled_messages_create(request: Request):
    body = await request.json()
    name = str(body.get("name") or "").strip()
    try:
        hour = int(body.get("cron_hour", 0))
        minute = int(body.get("cron_minute", 0))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "cron_hour/cron_minute 必须为整数"}, status_code=400)
    weekdays = str(body.get("weekdays", "0,1,2,3,4,5,6"))
    channel_id = str(body.get("channel_id") or "").strip()
    area_id = str(body.get("area_id") or "").strip()
    message_text = str(body.get("message_text") or "").strip()
    if not name or not channel_id or not area_id or not message_text:
        return JSONResponse({"ok": False, "error": "name/channel_id/area_id/message_text 不能为空"}, status_code=400)
    task_id = ScheduledMessageDB.create(
        name=name, cron_hour=hour, cron_minute=minute,
        channel_id=channel_id, area_id=area_id, message_text=message_text,
        weekdays=weekdays,
    )
    return JSONResponse({"ok": True, "id": task_id})


@router.put("/admin/api/scheduled-messages/{task_id}")
async def admin_scheduled_messages_update(task_id: int, request: Request):
    body = await request.json()
    updated = ScheduledMessageDB.update(task_id, **body)
    if not updated:
        return JSONResponse({"ok": False, "error": "未找到或无变更"}, status_code=404)
    return JSONResponse({"ok": True})


@router.delete("/admin/api/scheduled-messages/{task_id}")
def admin_scheduled_messages_delete(task_id: int):
    deleted = ScheduledMessageDB.delete(task_id)
    if not deleted:
        return JSONResponse({"ok": False, "error": "未找到"}, status_code=404)
    return JSONResponse({"ok": True})


@router.post("/admin/api/scheduled-messages/{task_id}/toggle")
def admin_scheduled_messages_toggle(task_id: int):
    result = ScheduledMessageDB.toggle(task_id)
    if result is None:
        return JSONResponse({"ok": False, "error": "未找到"}, status_code=404)
    return JSONResponse({"ok": True, "enabled": result})


# ---------------------------------------------------------------------------
# 消息统计 API
# ---------------------------------------------------------------------------

@router.get("/admin/api/message-stats/daily")
def admin_message_stats_daily(days: int = Query(14, ge=1, le=90)):
    daily = MessageStatsDB.get_all_daily(days=days)
    return JSONResponse({"ok": True, "daily": daily})


@router.get("/admin/api/message-stats/ranking")
def admin_message_stats_ranking(
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(10, ge=1, le=50),
    area_id: str = Query(""),
):
    # area_id 留空时跨全部域聚合，与日趋势/概览口径一致
    ranking = MessageStatsDB.get_user_ranking(area_id, days=days, limit=limit)
    resolver = get_resolver()
    for item in ranking:
        item["display_name"] = resolver.user(item["user_id"])
    return JSONResponse({"ok": True, "ranking": ranking})


@router.get("/admin/api/message-stats/overview")
def admin_message_stats_overview():
    return JSONResponse({
        "ok": True,
        "today_messages": MessageStatsDB.get_today_total(),
        "week_messages": MessageStatsDB.get_week_total(),
        "active_users_today": MessageStatsDB.get_active_users_today(),
    })


# ---------------------------------------------------------------------------
# 提醒查看 API
# ---------------------------------------------------------------------------

@router.get("/admin/api/reminders")
def admin_reminders_list():
    return JSONResponse({"ok": True, "items": ReminderDB.get_all_pending()})

__all__ = [name for name in globals() if not name.startswith("__")]
