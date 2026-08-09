"""
Redis 键名
"""

from __future__ import annotations

from domain.playback.web_command import (
    decode_web_command as decode_web_command,
)
from domain.playback.web_command import (
    encode_web_command as encode_web_command,
)

# --- 音乐播放 ---
# QUEUE/CURRENT/PLAY_STATE/PLAY_MODE 是历史基础名，只能交给 area_key 生成
# 域隔离键；对应旧全局键保留但不读取、不迁移、不删除。
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
    """生成域隔离的 Redis 键；空域一律拒绝。

    例：area_key("music:queue", "A1") -> "music:A1:queue"

    base 不含 ``':'`` 时按整体当作后缀，不再 IndexError。
    """
    normalized_area = str(area or "").strip()
    if not normalized_area:
        raise ValueError("播放域不能为空")
    prefix, _, suffix = base.partition(":")
    return (
        f"{prefix}:{normalized_area}:{suffix}"
        if suffix
        else f"{prefix}:{normalized_area}"
    )
