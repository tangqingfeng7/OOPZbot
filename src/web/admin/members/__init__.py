"""成员 / 频道 / 消息 / 域配置 管理路由。

历史上这些端点集中在单个 members.py（700+ 行）；现按资源拆分为子模块，这里把
各子路由聚合成一个 ``router`` 供 ``create_admin_router()`` 使用，并保留
``from web.admin.members import *`` 的兼容性（继续导出各端点函数）。
"""

from fastapi import APIRouter

from . import _area_configs, _bot_admins, _channels, _members, _messaging

_SUBMODULES = (_members, _bot_admins, _channels, _messaging, _area_configs)

router = APIRouter()
for _module in _SUBMODULES:
    router.include_router(_module.router)

# 兼容旧的 `from web.admin.members import *`：把各子模块定义的端点函数提升到包命名空间。
_g = globals()
_exported: list[str] = ["router"]
for _module in _SUBMODULES:
    for _name in dir(_module):
        if _name.startswith("_") or _name == "router":
            continue
        _obj = getattr(_module, _name)
        if callable(_obj) and getattr(_obj, "__module__", "").startswith("web.admin.members."):
            _g[_name] = _obj
            _exported.append(_name)

__all__ = _exported
