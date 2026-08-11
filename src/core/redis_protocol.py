from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol


class RedisPipeline(Protocol):
    """当前项目实际使用的 pipeline 子集。

    命令入队是同步链式调用（与 ``redis.asyncio`` 一致），只有 ``execute`` 走网络。
    """

    def get(self, key: str) -> RedisPipeline: ...

    def set(self, key: str, value: object, **kwargs: object) -> RedisPipeline: ...

    def rpush(self, key: str, *values: object) -> RedisPipeline: ...

    def llen(self, key: str) -> RedisPipeline: ...

    async def execute(self) -> list[object]: ...


class PlaybackCommandStore(Protocol):
    """播放应用服务写命令和修改队列所需的最小存储接口。"""

    async def set(
        self,
        key: str,
        value: object,
        ex: int | None = None,
        px: int | None = None,
        **kwargs: object,
    ) -> object: ...

    async def delete(self, *keys: str) -> int: ...

    async def rpush(self, key: str, *values: object) -> int: ...

    def pipeline(self, transaction: bool = False) -> RedisPipeline: ...


class RedisDataStore(PlaybackCommandStore, Protocol):
    """运行时共享异步 Redis/内存降级客户端的公共数据接口。"""

    async def ping(self) -> object: ...

    async def get(self, key: str) -> object | None: ...

    async def lpush(self, key: str, *values: object) -> int: ...

    async def lrange(self, key: str, start: int, end: int) -> list[object]: ...

    async def llen(self, key: str) -> int: ...

    async def lpop(self, key: str) -> object | None: ...

    async def lindex(self, key: str, index: int) -> object | None: ...

    async def blpop(self, key: str, timeout: int = 0) -> tuple[object, object] | None: ...

    async def scan(
        self,
        cursor: int = 0,
        match: str | None = None,
        count: int | None = None,
    ) -> tuple[int, list[str]]: ...


class RedisScriptExecutor(Protocol):
    """真实 Redis 执行 Lua 脚本所需的接口。"""

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object: ...


class RedisAdminClient(Protocol):
    """仅真实 Redis 提供的管理信息接口。"""

    async def info(self, section: str | None = None) -> Mapping[str, object]: ...

    async def dbsize(self) -> int: ...


def redis_text(value: object, *, field: str = "Redis value") -> str:
    """把 Redis 文本值规范化为 ``str``；拒绝意外对象。"""

    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, bytearray):
        return bytes(value).decode("utf-8")
    if isinstance(value, memoryview):
        return value.tobytes().decode("utf-8")
    raise TypeError(f"{field} 必须是 UTF-8 文本")


def redis_optional_text(value: object | None, *, field: str = "Redis value") -> str | None:
    """规范化可空文本值。"""

    return None if value is None else redis_text(value, field=field)


def redis_json_object(value: object, *, field: str = "Redis JSON") -> dict[str, object]:
    """解析并校验 Redis 中的 JSON 对象。"""

    payload = json.loads(redis_text(value, field=field))
    if not isinstance(payload, dict):
        raise ValueError(f"{field} 必须是 JSON 对象")
    return payload


def redis_int(value: object, *, field: str = "Redis integer") -> int:
    """解析 Redis 整数，同时拒绝 bool、浮点和任意对象的隐式转换。"""

    if isinstance(value, bool):
        raise TypeError(f"{field} 必须是整数")
    if isinstance(value, int):
        return value
    text = redis_text(value, field=field)
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是整数") from exc


__all__ = [
    "PlaybackCommandStore",
    "RedisAdminClient",
    "RedisDataStore",
    "RedisPipeline",
    "RedisScriptExecutor",
    "redis_int",
    "redis_json_object",
    "redis_optional_text",
    "redis_text",
]
