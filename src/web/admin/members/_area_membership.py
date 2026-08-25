"""Bot 域成员关系管理端点。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from web.admin.shared import (
    _invalidate_members_cache,
    _require_sender,
    read_json_body,
    require_sender,
)

router = APIRouter()


def _is_success(result: dict[str, Any]) -> bool:
    if result.get("error"):
        return False
    if "ok" in result:
        return bool(result["ok"])
    if "status" in result:
        return bool(result["status"])
    return True


@router.post("/admin/api/areas/leave")
@require_sender
async def admin_leave_area(request: Request):
    sender = _require_sender()
    body = await read_json_body(request)
    area = str(body.get("area") or "").strip()
    if not area:
        return JSONResponse({"ok": False, "error": "缺少域 ID"}, status_code=400)

    joined_areas = await sender.get_joined_areas(quiet=True)
    joined = next(
        (item for item in joined_areas if str(item.get("id") or "").strip() == area),
        None,
    )
    if joined is None:
        return JSONResponse(
            {"ok": False, "error": "Bot 当前未加入该域"},
            status_code=404,
        )

    result = await sender.leave_area(area)
    if not isinstance(result, dict) or not _is_success(result):
        error = result.get("error") if isinstance(result, dict) else "响应格式异常"
        return JSONResponse(
            {"ok": False, "error": f"退出域失败: {error or 'Oopz 拒绝了请求'}"},
            status_code=502,
        )

    area_name = str(joined.get("name") or area)

    from ._channels import _channels_cache
    from ._members import _area_meta_cache, _areas_cache

    _areas_cache.invalidate("all")
    _area_meta_cache.invalidate(area)
    _channels_cache.invalidate(area)
    _invalidate_members_cache()
    return {
        "ok": True,
        "area": area,
        "message": result.get("message") or f"Bot 已退出「{area_name}」",
    }


__all__ = ["router"]
