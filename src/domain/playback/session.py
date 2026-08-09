"""不可变播放会话快照。"""

from __future__ import annotations

from dataclasses import dataclass

from .web_command import AreaId


@dataclass(frozen=True, slots=True)
class PlaybackSessionSnapshot:
    """在同一把播放锁内捕获的域、频道和 generation。"""

    area: AreaId | None
    channel: str | None
    generation: int
