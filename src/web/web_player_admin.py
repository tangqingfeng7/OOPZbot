"""管理后台路由入口。

实际路由按功能拆在 ``web.admin`` 包中；这里保留 ``admin_router``
作为对 ``web.web_player`` 的稳定入口。
"""

from web.admin import create_admin_router

admin_router = create_admin_router()
