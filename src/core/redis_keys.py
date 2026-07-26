"""
Redis 键名
"""

from __future__ import annotations

# --- 音乐播放（全局键；带 area 时通过 area_key 生成域隔离键）---
QUEUE = "music:queue"
CURRENT = "music:current"
DEFAULT_CHANNEL = "music:default_channel"
PLAY_STATE = "music:play_state"
PLAY_MODE = "music:play_mode"
VOLUME = "music:volume"
WEB_COMMANDS = "music:web_commands"

# --- 后台会话 ---
ADMIN_SESSION = "music:admin_session"

# --- Cookie 名 ---
WEB_TOKEN_COOKIE = "web_token"
ADMIN_SESSION_COOKIE = "admin_session"


def area_key(base: str, area: str) -> str:
    """生成域隔离的 Redis 键。area 为空时回退到全局键。

    例：area_key("music:queue", "A1") -> "music:A1:queue"

    base 不含 ``':'`` 时按整体当作后缀，不再 IndexError。
    """
    if not area:
        return base
    prefix, _, suffix = base.partition(":")
    return f"{prefix}:{area}:{suffix}" if suffix else f"{prefix}:{area}"
