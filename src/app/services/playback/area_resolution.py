"""公共播放器与管理后台共用的播放域解析策略。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from domain.playback import AreaId


class PlaybackAreaUnavailable(LookupError):
    code = "playback_area_unavailable"
    message = "当前没有可用的播放域"


@dataclass(frozen=True, slots=True)
class AreaResolution:
    area: AreaId | None
    source: str

    @property
    def value(self) -> str:
        return self.area.value if self.area is not None else ""

    def require(self) -> AreaId:
        if self.area is None:
            raise PlaybackAreaUnavailable(self.message)
        return self.area

    @property
    def message(self) -> str:
        return PlaybackAreaUnavailable.message


class PlaybackAreaResolver:
    """封装两类明确且不回退到全局键的 area 选择策略。"""

    def __init__(
        self,
        *,
        active_area_reader: Callable[[], str],
        default_area_reader: Callable[[], str] | None = None,
        joined_area_reader: Callable[[], str] | None = None,
    ) -> None:
        self._active_area_reader = active_area_reader
        self._default_area_reader = default_area_reader or (lambda: "")
        self._joined_area_reader = joined_area_reader or (lambda: "")

    @staticmethod
    def _area(value: str) -> AreaId | None:
        normalized = str(value or "").strip()
        return AreaId(normalized) if normalized else None

    def public(self, explicit_area: str = "") -> AreaResolution:
        explicit = self._area(explicit_area)
        if explicit is not None:
            return AreaResolution(explicit, "explicit")
        active = self._area(self._active_area_reader())
        return AreaResolution(active, "active" if active is not None else "none")

    def admin(self) -> AreaResolution:
        active = self._area(self._active_area_reader())
        if active is not None:
            return AreaResolution(active, "active")
        default = self._area(self._default_area_reader())
        if default is not None:
            return AreaResolution(default, "default")
        joined = self._area(self._joined_area_reader())
        return AreaResolution(joined, "auto" if joined is not None else "none")
