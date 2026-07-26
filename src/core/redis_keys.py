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


_WEB_COMMAND_SEP = "|"


def encode_web_command(area: str, command: str) -> str:
    """把域 ID 编进 Web 控制命令载荷。

    命令队列是全局单键（Web 播放器是全局单令牌，拆键会让跨域命令永久无人消费），
    所以域信息只能随载荷走，由消费端校验。分隔符只切第一个，``notify:{json}``
    这种正文里带 ``|`` 的命令不受影响。

    哪些命令该带域看它作用在什么上：``next`` / ``stop`` / ``pause`` / ``resume``
    / ``seek`` / ``notify`` 作用于某个域正在播的那首歌，必须带域；``volume``
    作用于全局唯一的 Agora 输出设备，传空域表示不限定。
    """
    return f"{(area or '').strip()}{_WEB_COMMAND_SEP}{command}"


def decode_web_command(raw: str) -> tuple[str, str]:
    """还原 ``(域 ID, 命令)``。

    不含分隔符的旧载荷返回 ``("", 原文)`` —— 滚动升级期 Redis 里可能残留升级前
    写入的命令，这时无从判断归属，只能放行。等确认线上没有旧载荷后可以去掉这个
    兼容分支，否则跨域校验对残留载荷是失效的。
    """
    area, sep, command = (raw or "").partition(_WEB_COMMAND_SEP)
    if not sep:
        return "", raw or ""
    return area, command


def area_key(base: str, area: str) -> str:
    """生成域隔离的 Redis 键。area 为空时回退到全局键。

    例：area_key("music:queue", "A1") -> "music:A1:queue"

    base 不含 ``':'`` 时按整体当作后缀，不再 IndexError。
    """
    if not area:
        return base
    prefix, _, suffix = base.partition(":")
    return f"{prefix}:{area}:{suffix}" if suffix else f"{prefix}:{area}"
