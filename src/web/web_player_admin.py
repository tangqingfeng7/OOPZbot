"""管理后台路由入口。

实际路由按功能拆在 ``web.admin`` 包中；这里保留 ``admin_router``
作为对 ``web.web_player`` 和旧调用方的稳定入口。
"""

import sys
import types

from web.admin import create_admin_router
from web.admin import config as _config_module
from web.admin import music as _music_module
from web.admin import scheduler as _scheduler_module
from web.admin import shared as _shared_module
from web.admin.auth import *  # noqa: F401,F403
from web.admin.config import *  # noqa: F401,F403
from web.admin.members import *  # noqa: F401,F403
from web.admin.music import *  # noqa: F401,F403
from web.admin.pages import *  # noqa: F401,F403
from web.admin.plugins import *  # noqa: F401,F403
from web.admin.scheduler import *  # noqa: F401,F403
from web.admin.shared import *  # noqa: F401,F403

admin_router = create_admin_router()


class _AdminFacadeModule(types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for module in (_config_module, _music_module, _scheduler_module, _shared_module):
            if hasattr(module, name):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _AdminFacadeModule
