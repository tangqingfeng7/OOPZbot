from fastapi import APIRouter

from web.admin.shared import (
    JSONResponse,
    Request,
    _admin_enabled,
    _clear_admin_session_token,
    _set_admin_session_token,
    cfg,
    secrets,
)

router = APIRouter()

# ---------------------------------------------------------------------------
# 管理后台 API 路由
# ---------------------------------------------------------------------------

@router.post("/admin/api/login")
async def admin_login(request: Request):
    if not _admin_enabled():
        return JSONResponse({"ok": False, "error": "管理后台未启用"}, status_code=404)
    password = cfg.admin_password()
    if not password:
        return JSONResponse({"ok": False, "error": "未配置 admin_password"}, status_code=503)
    body = await request.json()
    submitted = str(body.get("password", ""))
    if not secrets.compare_digest(submitted, password):
        return JSONResponse({"ok": False, "error": "密码错误"}, status_code=401)
    token = secrets.token_urlsafe(24)
    _set_admin_session_token(token)
    ttl = cfg.admin_session_ttl_seconds()
    response = JSONResponse({"ok": True, "ttl": ttl})
    response.set_cookie(
        key=cfg.admin_cookie_name(),
        value=token,
        httponly=True,
        samesite="lax",
        secure=cfg.admin_cookie_secure(),
        max_age=ttl if ttl > 0 else None,
    )
    return response


@router.post("/admin/api/logout")
def admin_logout(request: Request):
    _clear_admin_session_token(request.cookies.get(cfg.admin_cookie_name(), ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(cfg.admin_cookie_name())
    return response


@router.get("/admin/api/me")
def admin_me():
    return JSONResponse({"ok": True, "role": "admin"})

__all__ = [name for name in globals() if not name.startswith("__")]
