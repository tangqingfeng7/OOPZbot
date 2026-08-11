"""频道管理：列表、增删改、设置、可访问成员、在线成员与语音频道监控。"""


from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from web.admin.shared import (
    TtlCache,
    _get_sender,
    _require_sender,
    _resolve_area_async,
    get_resolver,
    logger,
    read_json_body,
    require_sender,
)

router = APIRouter()

_CHANNELS_CACHE_TTL = 120.0
# 按域分槽：单槽缓存在多域间来回切时会互相挤掉，导致每次切域都要回源。
_channels_cache = TtlCache(_CHANNELS_CACHE_TTL)


@router.get("/admin/api/channels")
async def admin_channels_list(area: str = Query("")):
    """返回指定域的频道列表(含分组)。"""
    resolved_area = area.strip() or await _resolve_area_async()
    if not resolved_area:
        return JSONResponse({"ok": False, "error": "未找到可用域 ID"})

    cached = _channels_cache.get(resolved_area)
    if cached is not None:
        return JSONResponse(cached)

    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    groups = await sender.get_area_channels(area=resolved_area, quiet=True)
    channels = []
    for g in groups:
        group_name = g.get("name", "")
        for ch in g.get("channels") or []:
            ch_type = ch.get("type", "")
            channels.append({
                "id": ch.get("id", ""),
                "name": ch.get("name", ""),
                "group": group_name,
                "type": ch_type,
                "secret": bool(ch.get("secret")),
            })

    resp = {"ok": True, "channels": channels, "area": resolved_area}
    _channels_cache.set(resolved_area, resp)
    return JSONResponse(resp)


@router.post("/admin/api/channels/create")
@require_sender
async def admin_channel_create(request: Request):
    sender = _require_sender()
    body = await read_json_body(request)
    area = (body.get("area") or "").strip() or await _resolve_area_async()
    name = (body.get("name") or "").strip()
    ch_type = body.get("type", "text")
    group_id = (body.get("group_id") or "").strip()
    if not area or not name:
        return JSONResponse({"ok": False, "error": "area 和 name 不能为空"}, status_code=400)
    try:
        result = await sender.create_channel(area=area, name=name, channel_type=ch_type, group_id=group_id)
        if isinstance(result, dict) and "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]})
        _channels_cache.invalidate()
        return JSONResponse({"ok": True, "message": "频道已创建", "result": result if isinstance(result, dict) else {}})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.delete("/admin/api/channels/{channel_id}")
@require_sender
async def admin_channel_delete(channel_id: str, request: Request):
    sender = _require_sender()
    body = await read_json_body(request)
    area = (body.get("area") or "").strip() or await _resolve_area_async()
    if not area:
        return JSONResponse({"ok": False, "error": "area 不能为空"}, status_code=400)
    try:
        result = await sender.delete_channel(channel=channel_id, area=area)
        if isinstance(result, dict) and "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]})
        _channels_cache.invalidate()
        return JSONResponse({"ok": True, "message": "频道已删除"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.put("/admin/api/channels/{channel_id}")
@require_sender
async def admin_channel_update(channel_id: str, request: Request):
    sender = _require_sender()
    body = await read_json_body(request)
    area = (body.get("area") or "").strip() or await _resolve_area_async()
    name = (body.get("name") or "").strip()
    if not area:
        return JSONResponse({"ok": False, "error": "area 不能为空"}, status_code=400)
    try:
        result = await sender.update_channel(area=area, channel_id=channel_id, name=name)
        if isinstance(result, dict) and "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]})
        _channels_cache.invalidate()
        return JSONResponse({"ok": True, "message": "频道已更新"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/admin/api/channels/{channel_id}/settings")
@require_sender
async def admin_channel_settings(channel_id: str, area: str = Query("")):
    """获取频道的详细设置信息。"""
    sender = _require_sender()
    data = await sender.get_channel_setting_info(channel_id)
    if isinstance(data, dict) and "error" in data:
        return JSONResponse({"ok": False, "error": data["error"]})
    return JSONResponse({"ok": True, "settings": data})


@router.post("/admin/api/channels/{channel_id}/settings")
@require_sender
async def admin_channel_settings_edit(channel_id: str, request: Request):
    """编辑频道设置（名称、人数上限、慢速模式等）。"""
    sender = _require_sender()
    body = await read_json_body(request)
    area = (body.pop("area", "") or "").strip() or await _resolve_area_async()
    if not area:
        return JSONResponse({"ok": False, "error": "area 不能为空"}, status_code=400)
    try:
        result = await sender.update_channel(area=area, channel_id=channel_id, overrides=body)
        if isinstance(result, dict) and "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]})
        _channels_cache.invalidate()
        return JSONResponse({"ok": True, "message": "频道设置已保存"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/admin/api/channels/{channel_id}/accessible-members")
@require_sender
async def admin_channel_accessible_members(channel_id: str):
    """返回频道当前的可访问成员列表（含名称）。"""
    sender = _require_sender()
    setting = await sender.get_channel_setting_info(channel_id)
    if isinstance(setting, dict) and "error" in setting:
        return JSONResponse({"ok": False, "error": setting["error"]})
    uids = list(setting.get("accessibleMembers") or [])
    if not uids:
        return JSONResponse({"ok": True, "members": []})
    infos = await sender.get_person_infos_batch(uids)
    members = []
    for uid in uids:
        info = infos.get(uid, {})
        members.append({
            "uid": uid,
            "name": info.get("name") or info.get("nickname") or uid[:8],
            "avatar": info.get("avatar", ""),
        })
    return JSONResponse({"ok": True, "members": members})


@router.get("/admin/api/online-members")
@require_sender
async def admin_online_members(area: str = Query("")):
    """返回域内当前在线成员（用于私密频道成员选择）。"""
    sender = _require_sender()
    resolved_area = area.strip() or await _resolve_area_async()
    if not resolved_area:
        return JSONResponse({"ok": False, "error": "未找到可用域 ID"})
    all_members = []
    for page_start in range(0, 200, 50):
        result = await sender.get_area_members(
            area=resolved_area, offset_start=page_start,
            offset_end=page_start + 49, quiet=True,
        )
        if "error" in result:
            break
        batch = result.get("members") or []
        all_members.extend(batch)
        if len(batch) < 50:
            break
    online_members = [m for m in all_members if m.get("online", 0) == 1]
    uids = [m.get("uid", "") for m in online_members if m.get("uid")]
    if not uids:
        return JSONResponse({"ok": True, "members": []})
    infos = await sender.get_person_infos_batch(uids)
    members = []
    for m in online_members:
        uid = m.get("uid", "")
        if not uid:
            continue
        info = infos.get(uid, {})
        members.append({
            "uid": uid,
            "name": info.get("name") or info.get("nickname") or uid[:8],
        })
    return JSONResponse({"ok": True, "members": members})


@router.get("/admin/api/voice-channels")
async def admin_voice_channels(area: str = Query("")):
    """返回域内语音频道及其在线用户。"""
    resolved_area = area.strip() or await _resolve_area_async()
    if not resolved_area:
        return JSONResponse({"ok": False, "error": "未找到可用域 ID"})

    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    groups = await sender.get_area_channels(area=resolved_area, quiet=True)
    voice_info = {}
    for g in groups:
        for ch in g.get("channels") or []:
            ch_type = str(ch.get("type", "")).upper()
            if ch_type in ("VOICE", "AUDIO"):
                voice_info[ch.get("id", "")] = {
                    "name": ch.get("name", ""),
                    "group": g.get("name", ""),
                }

    channel_members = await sender.get_voice_channel_members(area=resolved_area)

    all_uids: list[str] = []
    for ch_id in voice_info:
        for m in channel_members.get(ch_id, []):
            uid = m.get("uid", m.get("id", "")) if isinstance(m, dict) else str(m)
            if uid and uid not in all_uids:
                all_uids.append(uid)
    person_map: dict = {}
    if all_uids:
        try:
            person_map = await sender.get_person_infos_batch(all_uids)
        except Exception:
            logger.debug("批量获取语音成员信息失败", exc_info=True)

    resolver = get_resolver()
    voice_channels = []
    for ch_id, info in voice_info.items():
        raw_members = channel_members.get(ch_id, [])
        users = []
        for m in raw_members:
            uid = m.get("uid", m.get("id", "")) if isinstance(m, dict) else str(m)
            if not uid:
                continue
            pi = person_map.get(uid, {})
            users.append({
                "uid": uid,
                "name": pi.get("name") or resolver.user(uid) or uid[:8],
                "avatar": pi.get("avatar", ""),
            })
        voice_channels.append({
            "id": ch_id,
            "name": info["name"],
            "group": info["group"],
            "users": users,
        })

    return JSONResponse({"ok": True, "voice_channels": voice_channels})


@router.post("/admin/api/voice-channels/dispatch")
@require_sender
async def admin_voice_dispatch(request: Request):
    """将用户从当前语音频道调度到指定语音频道。"""
    sender = _require_sender()
    body = await read_json_body(request)
    area = (body.get("area") or "").strip() or await _resolve_area_async()
    target = (body.get("target") or "").strip()
    to_channel = (body.get("to_channel") or "").strip()
    from_channel = (body.get("from_channel") or "").strip()
    if not area:
        return JSONResponse({"ok": False, "error": "area 不能为空"}, status_code=400)
    if not target or not to_channel:
        return JSONResponse({"ok": False, "error": "target 和 to_channel 不能为空"}, status_code=400)
    try:
        result = await sender.drag_member(target, to_channel, from_channel=from_channel or None, area=area)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    if isinstance(result, dict) and "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    return JSONResponse({"ok": True, "message": result.get("message", "已调度")})
