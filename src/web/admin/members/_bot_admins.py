"""Bot 管理员 UID 列表的增删查（持久化到 config.py）。"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from web.admin.shared import (
    cfg,
    get_resolver,
    logger,
)

router = APIRouter()


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
