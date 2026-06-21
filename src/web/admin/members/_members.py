"""成员管理：页面、域选择、成员列表/详情、禁言/封禁/角色等操作。"""

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
    get_resolver,
    logger,
    time,
)

router = APIRouter()


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
