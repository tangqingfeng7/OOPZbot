"""管理后台共享依赖和辅助函数。

历史上这些工具集中在单个 ``web/admin/shared.py``（1100+ 行）；现按主题拆分为
子模块（运行时访问器 / OOPZ 热更新 / 域上下文 / 调试工具 / 网易云 / B 站 /
页面渲染 / 会话令牌 / 概览快照），这里把它们重新聚合到包命名空间，并完整保留
原模块通过 ``from web.admin.shared import ...`` / ``import *`` 暴露的全部名字
（含被各路由模块依赖的第三方/标准库再导出符号，如 ``cfg``、``Statistics`` 等）。
"""
# pyright: reportMissingModuleSource=false

from __future__ import annotations

# --- 原 shared.py 顶层导入：保留这些再导出符号以维持调用方的导入兼容性 ---
import asyncio  # noqa: F401
import base64  # noqa: F401
import io  # noqa: F401
import json  # noqa: F401
import os  # noqa: F401
import secrets  # noqa: F401
import string  # noqa: F401
import sys  # noqa: F401
import time  # noqa: F401
from collections import deque  # noqa: F401
from http.cookies import SimpleCookie  # noqa: F401
from typing import Any, Optional  # noqa: F401
from urllib.parse import parse_qs, urlparse  # noqa: F401

from core.http_constants import HTTP_TIMEOUT_DEFAULT  # noqa: F401
from fastapi import APIRouter, Query, Request  # noqa: F401
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse  # noqa: F401

try:
    import requests

    RequestsException = requests.RequestException
except Exception:
    requests = None  # type: ignore[assignment]
    RequestsException = RuntimeError

try:
    import qrcode  # type: ignore[reportMissingModuleSource]  # noqa: F401
except Exception:
    qrcode = None  # type: ignore[assignment]

from core.database import (  # noqa: F401
    DB_PATH,
    MessageStatsDB,
    ReminderDB,
    ScheduledMessageDB,
    SongCache,
    Statistics,
    db_connection,
)
from core.logger_config import get_logger
from oopz.name_resolver import get_resolver  # noqa: F401
from core.queue_manager import (  # noqa: F401
    get_redis_client,
    _area_key,
    KEY_QUEUE,
    KEY_CURRENT,
    KEY_PLAY_STATE,
)
from services.scheduler_templates import get_scheduled_template, list_scheduled_templates  # noqa: F401
from web.web_link_token import (  # noqa: F401
    clear_token,
    ensure_token,
    get_active_area,
    get_token,
    set_token,
)

import web.web_player_config as cfg  # noqa: F401
from app.services.interaction.setup_diagnostics import SetupDiagnostics  # noqa: F401

logger = get_logger("WebPlayerAdmin")

# --- 各子模块的辅助函数/常量再导出（各子模块通过 __all__ 声明导出面）---
from ._runtime import *  # noqa: F401,F403,E402
from ._oopz import *  # noqa: F401,F403,E402
from ._area import *  # noqa: F401,F403,E402
from ._debug import *  # noqa: F401,F403,E402
from ._netease import *  # noqa: F401,F403,E402
from ._bilibili import *  # noqa: F401,F403,E402
from ._pages import *  # noqa: F401,F403,E402
from ._session import *  # noqa: F401,F403,E402
from ._snapshots import *  # noqa: F401,F403,E402

# 与原模块一致：导出全部非 dunder 顶层名字（包含上面的再导出符号与子模块辅助）。
__all__ = [name for name in globals() if not name.startswith("__")]
