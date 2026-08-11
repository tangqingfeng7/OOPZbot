"""成员管理：页面、域选择、成员列表/详情、禁言/封禁/角色等操作。"""

import asyncio
import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from web.admin.shared import (
    MessageStatsDB,
    TtlCache,
    _get_sender,
    _invalidate_members_cache,
    _members_resp_cache,
    _render_admin_page,
    _require_sender,
    _resolve_area_async,
    get_resolver,
    logger,
    read_json_body,
    require_sender,
)

router = APIRouter()


@router.get("/admin/members", response_class=HTMLResponse)
def admin_members_page():
    return _render_admin_page("members")


_AREAS_CACHE_TTL = 120.0
_AREA_META_CACHE_TTL = 120.0
_areas_cache = TtlCache(_AREAS_CACHE_TTL, maxsize=1)
# 按域分槽：单槽缓存在多域间来回切时会互相挤掉，导致每次切域都要回源。
_area_meta_cache = TtlCache(_AREA_META_CACHE_TTL)


@router.get("/admin/api/areas")
async def admin_areas_list():
    """返回 Bot 已加入的域列表,供前端域选择器使用。"""
    cached = _areas_cache.get("all")
    if cached is not None:
        return JSONResponse(cached)
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)
    areas = await sender.get_joined_areas(quiet=True)
    items = []
    for a in areas:
        items.append({
            "id": a.get("id", ""),
            "name": a.get("name", ""),
            "code": a.get("code", ""),
            "avatar": a.get("avatar", ""),
        })
    resp = {"ok": True, "areas": items}
    _areas_cache.set("all", resp)
    return JSONResponse(resp)


@router.get("/admin/api/areas/{area_id}/meta")
async def admin_area_meta(area_id: str):
    """返回域的表单辅助数据，如身份组列表。"""
    resolved_area = area_id.strip() or await _resolve_area_async()
    if not resolved_area:
        return JSONResponse({"ok": False, "error": "未找到可用域 ID"})

    cached = _area_meta_cache.get(resolved_area)
    if cached is not None:
        return JSONResponse(cached)

    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    area_info = await sender.get_area_info(area=resolved_area)
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
    _area_meta_cache.set(resolved_area, resp)
    return JSONResponse(resp)


ALL_AREAS = "__all__"
# 跨域聚合时每个域最多取这么多人。上限由平台决定：实测 offset_end 取到 100 正常，
# 再往上（149 / 199）服务端直接回「查询数量超过限制」，整域取数会失败。
_CROSS_AREA_FETCH_LIMIT = 100


async def _area_name_map() -> dict[str, str]:
    """域 ID -> 域名，用于给跨域结果标注来源。"""
    sender = _get_sender()
    if not sender:
        return {}
    try:
        areas = await sender.get_joined_areas(quiet=True)
    except Exception:
        logger.debug("获取已加入域失败", exc_info=True)
        return {}
    return {a.get("id", ""): (a.get("name") or a.get("id", "")) for a in areas if a.get("id")}


@router.get("/admin/api/members")
async def admin_members_list(
    offset: int = Query(0, ge=0),
    # 上限放宽到 500 是给跨域聚合用的：那条路径从内存池切片、不回源，一次多给点
    # 才能把各域成员一起铺出来。单域仍受平台分页限制约束（见 _CROSS_AREA_FETCH_LIMIT）。
    limit: int = Query(50, ge=1, le=500),
    keyword: str = Query(""),
    area: str = Query(""),
):
    if area.strip() == ALL_AREAS:
        return await _members_across_all_areas(offset=offset, limit=limit, keyword=keyword)
    limit = min(limit, _CROSS_AREA_FETCH_LIMIT)

    resolved_area = area.strip() if area.strip() else await _resolve_area_async()
    cache_key = f"{resolved_area}:{offset}:{limit}"
    if not keyword:
        cached = _members_resp_cache.get(cache_key)
        if cached is not None:
            return JSONResponse(cached)

    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    if not resolved_area:
        return JSONResponse({"ok": False, "error": "未找到可用域 ID，请检查配置"})

    result = await sender.get_area_members(area=resolved_area, offset_start=offset, offset_end=offset + limit - 1, quiet=True)
    if "error" in result:
        await asyncio.sleep(1)
        result = await sender.get_area_members(area=resolved_area, offset_start=offset, offset_end=offset + limit - 1)
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
            person_map = await sender.get_person_infos_batch(uids)
        except Exception:
            logger.debug("批量获取用户信息失败", exc_info=True)

    area_info = None
    try:
        area_info = await sender.get_area_info(area=resolved_area)
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
        _members_resp_cache.set(cache_key, resp_data)
    return JSONResponse(resp_data)


async def _members_across_all_areas(*, offset: int, limit: int, keyword: str) -> JSONResponse:
    """聚合全部已加入域的成员，用于全域搜索与合并列表。

    各域并发拉取，结果按域标注来源，再统一做关键词过滤与分页。没有关键词时也缓存，
    这样在合并列表里翻页、按域筛选都不必反复回源。
    """
    sender = _get_sender()
    if not sender:
        return JSONResponse({"ok": False, "error": "sender 未初始化"}, status_code=503)

    cache_key = f"{ALL_AREAS}:pool"
    pool = _members_resp_cache.get(cache_key)
    if pool is None:
        names = await _area_name_map()
        if not names:
            return JSONResponse({"ok": False, "error": "未找到任何已加入的域"})

        async def one(area_id: str) -> tuple[str, dict]:
            try:
                return area_id, await sender.get_area_members(
                    area=area_id, offset_start=0,
                    offset_end=_CROSS_AREA_FETCH_LIMIT - 1, quiet=True,
                )
            except Exception as exc:
                logger.debug("跨域取成员失败 area=%s: %s", area_id[:8], exc)
                return area_id, {"error": str(exc)}

        results = await asyncio.gather(*(one(aid) for aid in names))

        # 同一个人可能同时在多个域，按 uid 合并成一行并记录全部所属域。
        # 保持一人一行，既避免列表里重复出现，也让前端能一次展示所有域标签。
        by_uid: dict[str, dict] = {}
        failed: list[str] = []
        for area_id, result in results:
            if not isinstance(result, dict) or "error" in result:
                failed.append(names.get(area_id, area_id))
                continue
            members = result.get("members") or []
            uids = [m.get("uid", "") for m in members if m.get("uid")]
            person_map: dict = {}
            if uids:
                try:
                    person_map = await sender.get_person_infos_batch(uids)
                except Exception:
                    logger.debug("跨域批量取用户信息失败 area=%s", area_id[:8], exc_info=True)
            for m in members:
                uid = m.get("uid", "")
                if not uid:
                    continue
                membership = {
                    "areaId": area_id,
                    "areaName": names.get(area_id, area_id),
                    "role": m.get("role", 0),
                    "roleSort": m.get("roleSort", 0),
                }
                entry = by_uid.get(uid)
                if entry is None:
                    info = person_map.get(uid, {})
                    by_uid[uid] = {
                        "uid": uid,
                        "name": info.get("name") or uid[:8],
                        "avatar": info.get("avatar", ""),
                        "pid": info.get("pid", ""),
                        "online": m.get("online", 0) == 1,
                        "role": m.get("role", 0),
                        "roleName": "",
                        "roleSort": m.get("roleSort", 0),
                        "playingState": m.get("playingState", ""),
                        "displayType": m.get("displayType", ""),
                        "areas": [membership],
                    }
                    continue
                entry["areas"].append(membership)
                # 在任一域在线就算在线；权限取各域中最高的，避免合并后被低权限域盖掉
                if m.get("online", 0) == 1:
                    entry["online"] = True
                    if m.get("playingState"):
                        entry["playingState"] = m.get("playingState", "")
                        entry["displayType"] = m.get("displayType", "")
                if m.get("roleSort", 0) > entry["roleSort"]:
                    entry["roleSort"] = m.get("roleSort", 0)
                    entry["role"] = m.get("role", 0)

        merged = list(by_uid.values())
        for entry in merged:
            entry["areas"].sort(key=lambda a: -a["roleSort"])
            # 兼容单域视图的字段：主域取权限最高的那个
            entry["areaId"] = entry["areas"][0]["areaId"]
            entry["areaName"] = entry["areas"][0]["areaName"]
        pool = {"members": merged, "failed": failed}
        _members_resp_cache.set(cache_key, pool)

    from config import ADMIN_UIDS

    admin_set = set(ADMIN_UIDS)
    items = list(pool["members"])
    if keyword:
        kw = keyword.lower()
        items = [
            m for m in items
            if kw in m["name"].lower() or kw in m["uid"].lower() or kw in (m["pid"] or "").lower()
        ]

    total = len(items)
    page = items[offset:offset + limit]
    for m in page:
        m["is_bot_admin"] = m["uid"] in admin_set

    resp: dict = {
        "ok": True,
        "members": page,
        "total": total,
        "online": sum(1 for m in items if m["online"]),
        "offset": offset,
        "limit": limit,
        "crossArea": True,
    }
    if pool["failed"]:
        resp["partial"] = pool["failed"]
    return JSONResponse(resp)


@router.get("/admin/api/members/blocks")
@require_sender
async def admin_members_blocks(area: str = Query("")):
    sender = _require_sender()
    area = area.strip() or await _resolve_area_async()

    def _parse(data: dict, area_id: str, area_name: str) -> list[dict]:
        resolver = get_resolver()
        parsed = []
        for item in data.get("blocks") or []:
            if not isinstance(item, dict):
                continue
            raw_uid = item.get("uid") or item.get("person") or item.get("target") or ""
            if isinstance(raw_uid, dict):
                raw_uid = raw_uid.get("uid") or raw_uid.get("person") or ""
            uid = raw_uid if isinstance(raw_uid, str) else ""
            name = resolver.user(uid) if uid else ""
            parsed.append({
                "uid": uid,
                "name": name or uid[:12],
                "areaId": area_id,
                "areaName": area_name,
            })
        return parsed

    # 「全部域」不是真实域 ID，必须逐域查询后合并；封禁是按域生效的，
    # 每条都要带上来源域，否则解封会打到错误的域。
    if area == ALL_AREAS:
        names = await _area_name_map()
        if not names:
            return JSONResponse({"ok": True, "blocks": [], "error_hint": "未找到任何已加入的域"})

        async def one(area_id: str) -> tuple[str, dict]:
            try:
                return area_id, await sender.get_area_blocks(area=area_id)
            except Exception as exc:
                logger.debug("跨域取封禁失败 area=%s: %s", area_id[:8], exc)
                return area_id, {"error": str(exc)}

        blocks: list[dict] = []
        for area_id, data in await asyncio.gather(*(one(aid) for aid in names)):
            if isinstance(data, dict) and "error" not in data:
                blocks.extend(_parse(data, area_id, names.get(area_id, area_id)))
        return JSONResponse({"ok": True, "blocks": blocks, "crossArea": True})

    data = await sender.get_area_blocks(area=area) if area else {"error": "未找到可用域 ID"}
    if "error" in data:
        return JSONResponse({"ok": True, "blocks": [], "error_hint": data["error"]})
    names = await _area_name_map()
    return JSONResponse({"ok": True, "blocks": _parse(data, area, names.get(area, ""))})


@router.get("/admin/api/members/{uid}")
@require_sender
async def admin_member_detail(uid: str, area: str = Query("")):
    sender = _require_sender()
    area = area.strip() or await _resolve_area_async()
    detail = await sender.get_user_area_detail(uid, area=area) if area else {"error": "未找到域 ID"}
    if "error" in detail:
        return JSONResponse({"ok": False, "error": detail["error"]})
    person = await sender.get_person_detail(uid)
    assignable = await sender.get_assignable_roles(uid, area=area) if area else []
    default_area = area
    stats_data = await MessageStatsDB.get_user_ranking(
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
        if not isinstance(r, dict):
            continue
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


async def _extract_area(body: dict) -> str:
    return (body.get("area") or "").strip() or await _resolve_area_async()


@router.post("/admin/api/members/{uid}/mute")
@require_sender
async def admin_member_mute(uid: str, request: Request):
    sender = _require_sender()
    body = await read_json_body(request)
    area = await _extract_area(body)
    try:
        duration = int(body.get("duration", 5))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "duration 必须为整数"}, status_code=400)
    result = await sender.mute_user(uid, area=area, duration=duration)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已禁言")})


@router.post("/admin/api/members/{uid}/unmute")
@require_sender
async def admin_member_unmute(uid: str, request: Request):
    sender = _require_sender()
    body = await read_json_body(request)
    area = await _extract_area(body)
    result = await sender.unmute_user(uid, area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已解除禁言")})


@router.post("/admin/api/members/{uid}/mute-mic")
@require_sender
async def admin_member_mute_mic(uid: str, request: Request):
    sender = _require_sender()
    body = await read_json_body(request)
    area = await _extract_area(body)
    try:
        duration = int(body.get("duration", 10))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "duration 必须为整数"}, status_code=400)
    result = await sender.mute_mic(uid, area=area, duration=duration)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已禁麦")})


@router.post("/admin/api/members/{uid}/unmute-mic")
@require_sender
async def admin_member_unmute_mic(uid: str, request: Request):
    sender = _require_sender()
    body = await read_json_body(request)
    area = await _extract_area(body)
    result = await sender.unmute_mic(uid, area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已解除禁麦")})


@router.post("/admin/api/members/{uid}/kick")
@require_sender
async def admin_member_kick(uid: str, request: Request):
    sender = _require_sender()
    body = await read_json_body(request)
    area = await _extract_area(body)
    result = await sender.remove_from_area(uid, area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已踢出")})


@router.post("/admin/api/members/{uid}/block")
@require_sender
async def admin_member_block(uid: str, request: Request):
    sender = _require_sender()
    body = await read_json_body(request)
    area = await _extract_area(body)
    result = await sender.block_user_in_area(uid, area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已封禁")})


@router.post("/admin/api/members/{uid}/unblock")
@require_sender
async def admin_member_unblock(uid: str, request: Request):
    sender = _require_sender()
    body = await read_json_body(request)
    area = await _extract_area(body)
    result = await sender.unblock_user_in_area(uid, area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "已解封")})


@router.post("/admin/api/members/{uid}/role")
@require_sender
async def admin_member_role(uid: str, request: Request):
    sender = _require_sender()
    body = await read_json_body(request)
    area = await _extract_area(body)
    try:
        role_id = int(body.get("role_id", 0))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "role_id 必须为整数"}, status_code=400)
    action = str(body.get("action", "add"))
    if not role_id:
        return JSONResponse({"ok": False, "error": "role_id 不能为空"}, status_code=400)
    result = await sender.edit_user_role(uid, role_id, add=(action == "add"), area=area)
    if "error" in result:
        return JSONResponse({"ok": False, "error": result["error"]})
    _invalidate_members_cache()
    return JSONResponse({"ok": True, "message": result.get("message", "角色已更新")})
