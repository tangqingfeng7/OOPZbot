"""播放领域类型。"""

from .session import PlaybackSessionSnapshot
from .web_command import (
    AreaAction,
    AreaId,
    AreaWebCommand,
    GlobalAction,
    GlobalWebCommand,
    WebCommand,
    WebCommandDecodeError,
    decode_web_command,
    encode_web_command,
)

__all__ = [
    "AreaAction",
    "AreaId",
    "AreaWebCommand",
    "GlobalAction",
    "GlobalWebCommand",
    "PlaybackSessionSnapshot",
    "WebCommand",
    "WebCommandDecodeError",
    "decode_web_command",
    "encode_web_command",
]
