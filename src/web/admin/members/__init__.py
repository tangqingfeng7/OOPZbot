"""成员 / 频道 / 消息 / 域配置 管理路由。

历史上这些端点集中在单个 members.py（700+ 行）；现按资源拆分为子模块，这里把
各子路由聚合成一个 ``router`` 供 ``create_admin_router()`` 使用。
"""

from fastapi import APIRouter

from . import (
    _area_configs,
    _area_invites,
    _area_membership,
    _bot_admins,
    _channels,
    _members,
    _messaging,
)

_SUBMODULES = (
    _members,
    _bot_admins,
    _channels,
    _messaging,
    _area_configs,
    _area_invites,
    _area_membership,
)

router = APIRouter()
for _module in _SUBMODULES:
    router.include_router(_module.router)

__all__ = ["router"]
