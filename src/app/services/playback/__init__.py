"""播放应用服务。"""

from .area_resolution import (
    AreaResolution,
    PlaybackAreaResolver,
    PlaybackAreaUnavailable,
)
from .control_service import PlaybackControlService, playback_area_unavailable_result

__all__ = [
    "AreaResolution",
    "PlaybackAreaResolver",
    "PlaybackAreaUnavailable",
    "PlaybackControlService",
    "playback_area_unavailable_result",
]
