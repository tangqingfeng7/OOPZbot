from fastapi import APIRouter

from web.admin.shared import (
    HTMLResponse,
    JSONResponse,
    MessageStatsDB,
    Query,
    Request,
    _MEMBERS_RESP_TTL,
    _get_sender,
    _invalidate_members_cache,
    _members_resp_cache,
    _render_admin_page,
    _resolve_area,
    cfg,
    get_resolver,
    logger,
    time,
)

router = APIRouter()

# ---------------------------------------------------------------------------
# 成员管理页面 & API
# ---------------------------------------------------------------------------

@router.get("/admin/members", response_class=HTMLResponse)
def admin_members_page():
    return _render_admin_page("members")


_areas_cache: dict = {"data": None, "ts": 0.0}
_AREAS_CACHE_TTL = 120.0
_area_meta_cache: dict = {"data": None, "ts": 0.0, "area": ""}
_AREA_META_CACHE_TTL = 120.0


@router.get("/admin/api/areas")
def admin_areas_list():
    """返回 Bot 已加入的域列表,供前端域选择器使用。"""
    now = time.time()
    if _areas_cache["data"] and now - _areas_cache["ts"] < _AREAS_CACHE_TTL:
        return JSONResponse(_areas_cache["data"])
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    areas = sender.get_joined_areas(quiet=True)
    items = []
    for a in areas:
        items.append({
            "id": a.get("id", ""),
            "name": a.get("name", ""),
            "code": a.get("code", ""),
            "avatar": a.get("avatar", ""),
        })
    resp = {"ok": True, "areas": items}
    _areas_cache.update(data=resp, ts=now)
    return JSONResponse(resp)


@router.get("/admin/api/areas/{area_id}/meta")
def admin_area_meta(area_id: str):
    """返回域的表单辅助数据，如身份组列表。"""
    resolved_area = area_id.strip() or _resolve_area()
    if not resolved_area:
        return JSONResponse({"ok": False, "error": "未找到可用域 ID"})

    now = time.time()
    if (_area_meta_cache["data"] and _area_meta_cache["area"] == resolved_area
            and now - _area_meta_cache["ts"] < _AREA_META_CACHE_TTL):
        return JSONResponse(_area_meta_cache["data"])

    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    area_info = sender.get_area_info(area=resolved_area)
    if not isinstance(area_info, dict) or "error" in area_info:
        err = area_info.get("error") if isinstance(area_info, dict) else "获取域信息失败"
        return JSONResponse({"ok": False, "error": err or "获取域信息失败"})

    roles = []
    for role in area_info.get("roleList") or []:
        role_id = role.get("roleID")
        if role_id is None:
            continue
        roles.append({
            "id": str(role_id),
            "name": str(role.get("name", "") or ""),
            "sort": int(role.get("sort", 0) or 0),
            "type": int(role.get("type", 0) or 0),
        })
    roles.sort(key=lambda item: (-item["sort"], item["name"], item["id"]))

    resp = {
        "ok": True,
        "area": resolved_area,
        "home_page_channel_id": str(area_info.get("homePageChannelId", "") or ""),
        "roles": roles,
    }
    _area_meta_cache.update(data=resp, ts=now, area=resolved_area)
    return JSONResponse(resp)


@router.get("/admin/api/members")
def admin_members_list(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    keyword: str = Query(""),
    area: str = Query(""),
):
    resolved_area = area.strip() if area.strip() else _resolve_area()
    cache_key = f"{resolved_area}:{offset}:{limit}"
    now = time.time()
    if not keyword and _members_resp_cache["data"] and _members_resp_cache["key"] == cache_key \
            and now - _members_resp_cache["ts"] < _MEMBERS_RESP_TTL:
        return JSONResponse(_members_resp_cache["data"])

    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    if not resolved_area:
        return JSONResponse({"ok": False, "error": "未找到可用域 ID，请检查配置"})

    result = sender.get_area_members(area=resolved_area, offset_start=offset, offset_end=offset + limit - 1, quiet=True)
    if "error" in result:
        time.sleep(1)
        result = sender.get_area_members(area=resolved_area, offset_start=offset, offset_end=offset + limit - 1)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})

    members = result.get("members") or []
    total = result.get("totalCount") or result.get("userCount", len(members))
    online = result.get("onlineCount", 0)
    is_stale = result.get("stale", False)

    uids = [m.get("uid", "") for m in members if m.get("uid")]
    person_map: dict = {}
    if uids:
        try:
            person_map = sender.get_person_infos_batch(uids)
        except Exception:
            logger.debug("批量获取用户信息失败", exc_info=True)

    area_info = None
    try:
        area_info = sender.get_area_info(area=resolved_area)
    except Exception:
        logger.debug("获取域信息失败 (area=%s)", resolved_area[:8] if resolved_area else "")
    role_name_map: dict[int, str] = {}
    if area_info and isinstance(area_info, dict) and "error" not in area_info:
        for r in area_info.get("roleList") or []:
            rid = r.get("roleID")
            if rid is not None:
                role_name_map[int(rid)] = r.get("name", "")

    if keyword:
        kw = keyword.lower()
        filtered = []
        for m in members:
            uid = m.get("uid", "")
            pi = person_map.get(uid, {})
            name = pi.get("name", "") or uid[:8]
            if kw in name.lower() or kw in uid.lower() or kw in (pi.get("pid") or "").lower():
                filtered.append(m)
        members = filtered
        total = len(filtered)

    from config import ADMIN_UIDS
    admin_set = set(ADMIN_UIDS)

    items = []
    for m in members:
        uid = m.get("uid", "")
        pi = person_map.get(uid, {})
        role_id = m.get("role", 0)
        items.append({
            "uid": uid,
            "name": pi.get("name") or uid[:8],
            "avatar": pi.get("avatar", ""),
            "pid": pi.get("pid", ""),
            "online": m.get("online", 0) == 1,
            "role": role_id,
            "roleName": role_name_map.get(int(role_id), "") if role_id else "",
            "roleSort": m.get("roleSort", 0),
            "playingState": m.get("playingState", ""),
            "displayType": m.get("displayType", ""),
            "is_bot_admin": uid in admin_set,
        })
    resp_data: dict = {
        "ok": True,
        "members": items,
        "total": total,
        "online": online,
        "offset": offset,
        "limit": limit,
    }
    if is_stale:
        resp_data["stale"] = True
    if not keyword:
        _members_resp_cache.update(data=resp_data, ts=time.time(), key=cache_key)
    return JSONResponse(resp_data)


@router.get("/admin/api/members/blocks")
def admin_members_blocks(area: str = Query("")):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    area = area.strip() or _resolve_area()
    data = sender.get_area_blocks(area=area) if area else {"error": "未找到可用域 ID"}
    if "error" in data:
        return JSONResponse({"ok": True, "blocks": [], "error_hint": data["error"]})
    resolver = get_resolver()
    blocks = []
    for item in data.get("blocks") or []:
        uid = item.get("uid") or item.get("person") or item.get("target") or ""
        if isinstance(uid, dict):
            uid = uid.get("uid") or uid.get("person") or ""
        name = resolver.user(uid) if isinstance(uid, str) and uid else ""
        blocks.append({"uid": uid, "name": name or uid[:12]})
    return JSONResponse({"ok": True, "blocks": blocks})


@router.get("/admin/api/members/{uid}")
def admin_member_detail(uid: str, area: str = Query("")):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    area = area.strip() or _resolve_area()
    detail = sender.get_user_area_detail(uid, area=area) if area else {"error": "未找到域 ID"}
    if "error" in detail:
        return JSONResponse({"ok": False, "error": detail["error"]})
    person = sender.get_person_detail(uid)
    assignable = sender.get_assignable_roles(uid, area=area) if area else []
    default_area = area
    stats_data = MessageStatsDB.get_user_ranking(
        area_id=default_area,
        days=7,
        limit=100,
    )
    user_msg_count = 0
    for s in stats_data:
        if s.get("user_id") == uid:
            user_msg_count = s.get("total", 0)
            break

    person_data: dict = {}
    if "error" not in person:
        person_data = {
            "name": person.get("name") or person.get("nickname") or uid[:8],
            "avatar": person.get("avatar") or "",
            "pid": person.get("pid") or person.get("userCommonId") or "",
            "online": bool(person.get("online")),
            "introduction": person.get("introduction") or "",
        }

    role_list = detail.get("list") or []
    roles_out = []
    for r in role_list:
        roles_out.append({
            "roleID": r.get("roleID"),
            "name": r.get("name", ""),
        })

    disable_text_to = detail.get("disableTextTo", 0)
    disable_voice_to = detail.get("disableVoiceTo", 0)
    now_ms = int(time.time() * 1000)
    is_muted = isinstance(disable_text_to, (int, float)) and int(disable_text_to) > now_ms
    is_mic_muted = isinstance(disable_voice_to, (int, float)) and int(disable_voice_to) > now_ms

    from config import ADMIN_UIDS
    is_bot_admin = uid in ADMIN_UIDS

    return JSONResponse({
        "ok": True,
        "uid": uid,
        "person": person_data,
        "roles": roles_out,
        "muted": is_muted,
        "muted_until": int(disable_text_to) if is_muted else 0,
        "mic_muted": is_mic_muted,
        "mic_muted_until": int(disable_voice_to) if is_mic_muted else 0,
        "assignable_roles": assignable if isinstance(assignable, list) else [],
        "messages_7d": user_msg_count,
        "is_bot_admin": is_bot_admin,
    })


def _extract_area(body: dict) -> str:
    return (body.get("area") or "").strip() or _resolve_area()


@router.post("/admin/api/members/{uid}/mute")
async def admin_member_mute(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    try:
        duration = int(body.get("duration", 5))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "duration 必须为整数"}, status_code=400)
    result = sender.mute_user(uid, area=area, duration=duration)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已禁言")})


@router.post("/admin/api/members/{uid}/unmute")
async def admin_member_unmute(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    result = sender.unmute_user(uid, area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已解除禁言")})


@router.post("/admin/api/members/{uid}/mute-mic")
async def admin_member_mute_mic(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    try:
        duration = int(body.get("duration", 10))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "duration 必须为整数"}, status_code=400)
    result = sender.mute_mic(uid, area=area, duration=duration)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已禁麦")})


@router.post("/admin/api/members/{uid}/unmute-mic")
async def admin_member_unmute_mic(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    result = sender.unmute_mic(uid, area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已解除禁麦")})


@router.post("/admin/api/members/{uid}/kick")
async def admin_member_kick(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    result = sender.remove_from_area(uid, area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已踢出")})


@router.post("/admin/api/members/{uid}/block")
async def admin_member_block(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    result = sender.block_user_in_area(uid, area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已封禁")})


@router.post("/admin/api/members/{uid}/unblock")
async def admin_member_unblock(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    result = sender.unblock_user_in_area(uid, area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已解封")})


@router.get("/admin/api/bot-admins")
def admin_bot_admins_list():
    """返回当前 Bot 管理员 UID 列表。"""
    from config import ADMIN_UIDS
    resolver = get_resolver()
    items = []
    for uid in ADMIN_UIDS:
        items.append({"uid": uid, "name": resolver.user(uid) or uid[:12]})
    return JSONResponse({"ok": True, "admins": items, "uids": list(ADMIN_UIDS)})


@router.post("/admin/api/bot-admins")
async def admin_bot_admins_add(request: Request):
    """将指定 UID 添加为 Bot 管理员。"""
    import config as _cfg
    body = await request.json()
    uid = str(body.get("uid", "")).strip()
    if not uid:
        return JSONResponse({"ok": False, "error": "uid 不能为空"}, status_code=400)
    if uid in _cfg.ADMIN_UIDS:
        return JSONResponse({"ok": True, "message": "该用户已是管理员"})
    _cfg.ADMIN_UIDS.append(uid)
    try:
        _persist_admin_uids(_cfg.ADMIN_UIDS)
    except Exception as exc:
        _cfg.ADMIN_UIDS.remove(uid)
        logger.exception("保存 Bot 管理员列表失败")
        return JSONResponse({"ok": False, "error": f"保存 config.py 失败: {exc}"}, status_code=500)
    resolver = get_resolver()
    name = resolver.user(uid) or uid[:12]
    logger.info("Bot 管理员已添加: %s (%s)", name, uid[:12])
    return JSONResponse({"ok": True, "message": f"已将 {name} 设为管理员，并写入 config.py"})


@router.delete("/admin/api/bot-admins/{uid}")
def admin_bot_admins_remove(uid: str):
    """移除指定 UID 的 Bot 管理员权限。"""
    import config as _cfg
    uid = uid.strip()
    if uid not in _cfg.ADMIN_UIDS:
        return JSONResponse({"ok": False, "error": "该用户不是管理员"}, status_code=404)
    _cfg.ADMIN_UIDS.remove(uid)
    try:
        _persist_admin_uids(_cfg.ADMIN_UIDS)
    except Exception as exc:
        _cfg.ADMIN_UIDS.append(uid)
        logger.exception("保存 Bot 管理员列表失败")
        return JSONResponse({"ok": False, "error": f"保存 config.py 失败: {exc}"}, status_code=500)
    resolver = get_resolver()
    name = resolver.user(uid) or uid[:12]
    logger.info("Bot 管理员已移除: %s (%s)", name, uid[:12])
    return JSONResponse({"ok": True, "message": f"已移除 {name} 的管理员权限，并写入 config.py"})


def _persist_admin_uids(uids: list) -> None:
    cfg.persist_admin_uids(list(uids))


@router.post("/admin/api/members/{uid}/role")
async def admin_member_role(uid: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = _extract_area(body)
    try:
        role_id = int(body.get("role_id", 0))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "role_id 必须为整数"}, status_code=400)
    action = str(body.get("action", "add"))
    if not role_id:
        return JSONResponse({"ok": False, "error": "role_id 不能为空"}, status_code=400)
    result = sender.edit_user_role(uid, role_id, add=(action == "add"), area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "角色已更新")})


# ---------------------------------------------------------------------------
# 频道列表 & 发送消息/公告 API
# ---------------------------------------------------------------------------

_channels_cache: dict = {"data": None, "ts": 0.0, "area": ""}
_CHANNELS_CACHE_TTL = 120.0


@router.get("/admin/api/channels")
def admin_channels_list(area: str = Query("")):
    """返回指定域的频道列表(含分组)。"""
    resolved_area = area.strip() or _resolve_area()
    if not resolved_area:
        return JSONResponse({"ok": False, "error": "未找到可用域 ID"})

    now = time.time()
    if (_channels_cache["data"] and _channels_cache["area"] == resolved_area
            and now - _channels_cache["ts"] < _CHANNELS_CACHE_TTL):
        return JSONResponse(_channels_cache["data"])

    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    groups = sender.get_area_channels(area=resolved_area, quiet=True)
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
    _channels_cache.update(data=resp, ts=now, area=resolved_area)
    return JSONResponse(resp)


@router.post("/admin/api/send-message")
async def admin_send_message(request: Request):
    """发送普通消息到指定频道。"""
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    body = await request.json()
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
async def admin_send_announcement(request: Request):
    """发送公告样式消息到指定频道。"""
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    body = await request.json()
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


# ---------------------------------------------------------------------------
# 域配置管理 API (area_configs CRUD)
# ---------------------------------------------------------------------------

@router.get("/admin/api/area-configs")
def admin_area_configs_list():
    """返回所有域的独立配置。"""
    from core.area_config import get_area_registry
    reg = get_area_registry()
    configs = reg.export_all()
    return JSONResponse({"ok": True, "configs": configs})


@router.get("/admin/api/area-configs/{area_id}")
def admin_area_config_get(area_id: str):
    from core.area_config import get_area_registry, AreaConfigRegistry
    reg = get_area_registry()
    if not reg.is_configured(area_id):
        return JSONResponse({"ok": True, "configured": False, "config": {}})
    c = reg.get(area_id)
    return JSONResponse({"ok": True, "configured": True, "config": AreaConfigRegistry.config_to_dict(c)})


@router.post("/admin/api/area-configs/{area_id}")
async def admin_area_config_save(area_id: str, request: Request):
    """创建或更新域配置并持久化。"""
    body = await request.json()
    area_id = area_id.strip()
    if not area_id:
        return JSONResponse({"ok": False, "error": "area_id 不能为空"}, status_code=400)

    from core.area_config import get_area_registry, AreaConfigRegistry
    reg = get_area_registry()
    reg.update_config(area_id, body)

    saved = cfg.read_area_overrides()
    saved[area_id] = body
    cfg.write_area_overrides(saved)

    return JSONResponse({"ok": True, "config": AreaConfigRegistry.config_to_dict(reg.get(area_id))})


@router.delete("/admin/api/area-configs/{area_id}")
def admin_area_config_delete(area_id: str):
    """删除域的独立配置。"""
    area_id = area_id.strip()
    from core.area_config import get_area_registry
    reg = get_area_registry()
    removed = reg.remove_config(area_id)

    saved = cfg.read_area_overrides()
    saved.pop(area_id, None)
    cfg.write_area_overrides(saved)

    return JSONResponse({"ok": True, "removed": removed})


# ---------------------------------------------------------------------------
# 频道管理 API (创建 / 删除 / 修改)
# ---------------------------------------------------------------------------

@router.post("/admin/api/channels/create")
async def admin_channel_create(request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = (body.get("area") or "").strip() or _resolve_area()
    name = (body.get("name") or "").strip()
    ch_type = body.get("type", "text")
    group_id = (body.get("group_id") or "").strip()
    if not area or not name:
        return JSONResponse({"ok": False, "error": "area 和 name 不能为空"}, status_code=400)
    try:
        result = sender.create_channel(area=area, name=name, channel_type=ch_type, group_id=group_id)
        if isinstance(result, dict) and "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]})
        _channels_cache.update(data=None, ts=0.0, area="")
        return JSONResponse({"ok": True, "message": "频道已创建", "result": result if isinstance(result, dict) else {}})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.delete("/admin/api/channels/{channel_id}")
async def admin_channel_delete(channel_id: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = (body.get("area") or "").strip() or _resolve_area()
    if not area:
        return JSONResponse({"ok": False, "error": "area 不能为空"}, status_code=400)
    try:
        result = sender.delete_channel(channel=channel_id, area=area)
        if isinstance(result, dict) and "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]})
        _channels_cache.update(data=None, ts=0.0, area="")
        return JSONResponse({"ok": True, "message": "频道已删除"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.put("/admin/api/channels/{channel_id}")
async def admin_channel_update(channel_id: str, request: Request):
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = (body.get("area") or "").strip() or _resolve_area()
    name = (body.get("name") or "").strip()
    if not area:
        return JSONResponse({"ok": False, "error": "area 不能为空"}, status_code=400)
    try:
        result = sender.update_channel(area=area, channel_id=channel_id, name=name)
        if isinstance(result, dict) and "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]})
        _channels_cache.update(data=None, ts=0.0, area="")
        return JSONResponse({"ok": True, "message": "频道已更新"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# 频道设置 API (读取 / 编辑)
# ---------------------------------------------------------------------------

@router.get("/admin/api/channels/{channel_id}/settings")
def admin_channel_settings(channel_id: str, area: str = Query("")):
    """获取频道的详细设置信息。"""
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    data = sender.get_channel_setting_info(channel_id)
    if isinstance(data, dict) and "error" in data:
        return JSONResponse({"ok": False, "error": data["error"]})
    return JSONResponse({"ok": True, "settings": data})


@router.post("/admin/api/channels/{channel_id}/settings")
async def admin_channel_settings_edit(channel_id: str, request: Request):
    """编辑频道设置（名称、人数上限、慢速模式等）。"""
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    body = await request.json()
    area = (body.pop("area", "") or "").strip() or _resolve_area()
    if not area:
        return JSONResponse({"ok": False, "error": "area 不能为空"}, status_code=400)
    try:
        result = sender.update_channel(area=area, channel_id=channel_id, overrides=body)
        if isinstance(result, dict) and "error" in result:
            return JSONResponse({"ok": False, "error": result["error"]})
        _channels_cache.update(data=None, ts=0.0, area="")
        return JSONResponse({"ok": True, "message": "频道设置已保存"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# 频道可访问成员 API (私密频道)
# ---------------------------------------------------------------------------

@router.get("/admin/api/channels/{channel_id}/accessible-members")
def admin_channel_accessible_members(channel_id: str):
    """返回频道当前的可访问成员列表（含名称）。"""
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    setting = sender.get_channel_setting_info(channel_id)
    if isinstance(setting, dict) and "error" in setting:
        return JSONResponse({"ok": False, "error": setting["error"]})
    uids = list(setting.get("accessibleMembers") or [])
    if not uids:
        return JSONResponse({"ok": True, "members": []})
    infos = sender.get_person_infos_batch(uids)
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
def admin_online_members(area: str = Query("")):
    """返回域内当前在线成员（用于私密频道成员选择）。"""
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    resolved_area = area.strip() or _resolve_area()
    if not resolved_area:
        return JSONResponse({"ok": False, "error": "未找到可用域 ID"})
    all_members = []
    for page_start in range(0, 200, 50):
        result = sender.get_area_members(
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
    infos = sender.get_person_infos_batch(uids)
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


# ---------------------------------------------------------------------------
# 语音频道监控 API
# ---------------------------------------------------------------------------

@router.get("/admin/api/voice-channels")
def admin_voice_channels(area: str = Query("")):
    """返回域内语音频道及其在线用户。"""
    resolved_area = area.strip() or _resolve_area()
    if not resolved_area:
        return JSONResponse({"ok": False, "error": "未找到可用域 ID"})

    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    groups = sender.get_area_channels(area=resolved_area, quiet=True)
    voice_info = {}
    for g in groups:
        for ch in g.get("channels") or []:
            ch_type = str(ch.get("type", "")).upper()
            if ch_type in ("VOICE", "AUDIO"):
                voice_info[ch.get("id", "")] = {
                    "name": ch.get("name", ""),
                    "group": g.get("name", ""),
                }

    channel_members = sender.get_voice_channel_members(area=resolved_area)

    resolver = get_resolver()
    voice_channels = []
    for ch_id, info in voice_info.items():
        raw_members = channel_members.get(ch_id, [])
        users = []
        for m in raw_members:
            uid = m.get("uid", m.get("id", "")) if isinstance(m, dict) else str(m)
            if uid:
                users.append({"uid": uid, "name": resolver.user(uid) or uid[:8]})
        voice_channels.append({
            "id": ch_id,
            "name": info["name"],
            "group": info["group"],
            "users": users,
        })

    return JSONResponse({"ok": True, "voice_channels": voice_channels})

__all__ = [name for name in globals() if not name.startswith("__")]
