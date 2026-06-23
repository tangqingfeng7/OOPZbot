"""管理后台请求体解析辅助。

各路由历史上各自 ``await request.json()``：config.py 用 ``try/except`` 降级为
空 dict，再交给字段校验返回带语义的 400；而 members/_channels.py 直接裸调，
畸形或空 body 会让 ``json.JSONDecodeError`` 冒泡成 500。这里统一成一个解析入口，
失败时一律降级为空 dict，让下游字段校验产出一致的 4xx 响应。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request


async def read_json_body(request: Request) -> dict[str, Any]:
    """解析请求体为 dict；非 JSON 对象或畸形/空 body 时降级为空 dict。

    与既有路由保持一致：不在此处直接返回错误响应，而是把校验交给各处理器
    的字段检查（如 ``if not area``），从而产出带语义的 400 而非裸 500。
    """
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


__all__ = [
    "read_json_body",
]
