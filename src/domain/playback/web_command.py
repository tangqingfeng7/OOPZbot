"""Redis Web 播放命令的严格、版本化领域协议。"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, TypeAlias

_PROTOCOL_VERSION = 1
_AREA_ACTIONS = frozenset({"next", "stop", "pause", "resume", "seek", "notify"})
_GLOBAL_ACTIONS = frozenset({"volume"})


class WebCommandDecodeError(ValueError):
    """载荷不符合当前 Web 命令协议。"""


@dataclass(frozen=True, slots=True)
class AreaId:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise ValueError("播放域必须是字符串")
        normalized = self.value.strip()
        if not normalized:
            raise ValueError("播放域不能为空")
        if len(normalized) > 128:
            raise ValueError("播放域标识过长")
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        return self.value


AreaAction: TypeAlias = Literal["next", "stop", "pause", "resume", "seek", "notify"]
GlobalAction: TypeAlias = Literal["volume"]


@dataclass(frozen=True, slots=True)
class AreaWebCommand:
    area: AreaId
    action: AreaAction
    payload: Mapping[str, object]
    version: int = _PROTOCOL_VERSION
    scope: Literal["area"] = "area"

    def __post_init__(self) -> None:
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version != _PROTOCOL_VERSION
            or self.scope != "area"
        ):
            raise ValueError("Web 命令版本或作用域无效")
        if not isinstance(self.action, str) or self.action not in _AREA_ACTIONS:
            raise ValueError(f"未知域命令: {self.action}")
        validated = _validate_area_payload(self.action, self.payload)
        object.__setattr__(self, "payload", MappingProxyType(validated))


@dataclass(frozen=True, slots=True)
class GlobalWebCommand:
    action: GlobalAction
    payload: Mapping[str, object]
    version: int = _PROTOCOL_VERSION
    scope: Literal["global"] = "global"

    def __post_init__(self) -> None:
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version != _PROTOCOL_VERSION
            or self.scope != "global"
        ):
            raise ValueError("Web 命令版本或作用域无效")
        if not isinstance(self.action, str) or self.action not in _GLOBAL_ACTIONS:
            raise ValueError(f"未知全局命令: {self.action}")
        validated = _validate_global_payload(self.action, self.payload)
        object.__setattr__(self, "payload", MappingProxyType(validated))


WebCommand: TypeAlias = AreaWebCommand | GlobalWebCommand


def _exact_keys(payload: dict, expected: set[str], context: str) -> None:
    if set(payload) != expected:
        raise ValueError(f"{context} payload 字段无效")


def _validate_area_payload(action: str, raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{action} payload 必须是对象")
    payload = dict(raw)
    if action in {"next", "stop", "pause", "resume"}:
        _exact_keys(payload, set(), action)
        return {}
    if action == "seek":
        _exact_keys(payload, {"time"}, action)
        raw_value = payload["time"]
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ValueError("seek.time 必须是数字")
        value = float(raw_value)
        if not math.isfinite(value) or value < 0:
            raise ValueError("seek.time 必须是非负有限数字")
        return {"time": value}
    if action == "notify":
        _exact_keys(payload, {"name", "artists", "position"}, action)
        name = payload["name"]
        artists = payload["artists"]
        if not isinstance(name, str) or not isinstance(artists, str):
            raise ValueError("notify.name 和 notify.artists 必须是字符串")
        if len(name) > 500 or len(artists) > 500:
            raise ValueError("notify 文本过长")
        position = payload["position"]
        if isinstance(position, bool) or not isinstance(position, int):
            raise ValueError("notify.position 必须是整数")
        if position < 0:
            raise ValueError("notify.position 必须非负")
        return {"name": name, "artists": artists, "position": position}
    raise ValueError(f"未知域命令: {action}")


def _validate_global_payload(action: str, raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{action} payload 必须是对象")
    payload = dict(raw)
    if action == "volume":
        _exact_keys(payload, {"value"}, action)
        value = payload["value"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("volume.value 必须是整数")
        if value < 0 or value > 100:
            raise ValueError("volume.value 必须位于 0..100")
        return {"value": value}
    raise ValueError(f"未知全局命令: {action}")


def encode_web_command(command: WebCommand) -> str:
    payload = {
        "version": command.version,
        "scope": command.scope,
        "action": command.action,
        "payload": dict(command.payload),
    }
    if isinstance(command, AreaWebCommand):
        payload["area"] = command.area.value
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WebCommandDecodeError(f"Web 命令包含重复字段: {key}")
        result[key] = value
    return result


def _reject_nonstandard_json_constant(value: str) -> object:
    raise WebCommandDecodeError(f"Web 命令包含非标准 JSON 数值: {value}")


def decode_web_command(raw: str | bytes) -> WebCommand:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WebCommandDecodeError("Web 命令不是 UTF-8") from exc
    try:
        data = json.loads(
            str(raw),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except WebCommandDecodeError:
        raise
    except (json.JSONDecodeError, TypeError) as exc:
        raise WebCommandDecodeError("Web 命令不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise WebCommandDecodeError("Web 命令必须是 JSON 对象")

    scope = data.get("scope")
    expected = (
        {"version", "scope", "area", "action", "payload"}
        if scope == "area"
        else {"version", "scope", "action", "payload"}
    )
    if set(data) != expected:
        raise WebCommandDecodeError("Web 命令字段无效")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != _PROTOCOL_VERSION:
        raise WebCommandDecodeError("不支持的 Web 命令版本")

    try:
        if scope == "area":
            if not isinstance(data["area"], str):
                raise ValueError("播放域必须是字符串")
            return AreaWebCommand(
                area=AreaId(data["area"]),
                action=data["action"],
                payload=data["payload"],
            )
        if scope == "global":
            return GlobalWebCommand(
                action=data["action"],
                payload=data["payload"],
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise WebCommandDecodeError(str(exc)) from exc
    raise WebCommandDecodeError("未知 Web 命令作用域")


__all__ = [
    "AreaAction",
    "AreaId",
    "AreaWebCommand",
    "GlobalAction",
    "GlobalWebCommand",
    "WebCommand",
    "WebCommandDecodeError",
    "decode_web_command",
    "encode_web_command",
]
