from __future__ import annotations

import fnmatch
import json
import math
import random
import threading
import time
import uuid
from typing import cast

import redis

from config import REDIS_CONFIG
from core.logger_config import get_logger
from core.redis_keys import (
    CURRENT as KEY_CURRENT,
)
from core.redis_keys import (
    DEFAULT_CHANNEL as KEY_DEFAULT_CHANNEL,
)
from core.redis_keys import (
    PLAY_MODE as KEY_PLAY_MODE,
)
from core.redis_keys import (
    PLAY_STATE as KEY_PLAY_STATE,
)
from core.redis_keys import (
    QUEUE as KEY_QUEUE,
)
from core.redis_keys import (
    area_key as _area_key,
)
from core.redis_protocol import (
    RedisDataStore,
    RedisPipeline,
    RedisScriptExecutor,
    redis_int,
    redis_json_object,
    redis_optional_text,
)

logger = get_logger("QueueManager")

_redis_client: RedisDataStore | None = None
_redis_lock = threading.Lock()
_redis_condition = threading.Condition(_redis_lock)
# 处于内存降级状态时，每隔该秒数尝试重连一次真实 Redis。
_REDIS_RETRY_INTERVAL = 30.0
# 等待某一次恢复探测的最长时间。它略大于默认的建连 + 读取超时；
# DNS/底层驱动如果不遵守 socket 超时，调用方仍能有界返回 fallback。
_REDIS_PROBE_WAIT_TIMEOUT = 10.0
_last_redis_retry = 0.0
_redis_generation = 0
_redis_probe_in_flight = False
_redis_probe_token: object | None = None
_redis_probe_client: RedisDataStore | None = None

_QUEUE_REMOVE_AT_LUA = """
local key = KEYS[1]
local idx = tonumber(ARGV[1])
local marker = ARGV[2]
if idx < 0 or idx >= redis.call('llen', key) then
    return -1
end
redis.call('lset', key, idx, marker)
redis.call('lrem', key, 1, marker)
return 0
"""

_QUEUE_MOVE_TO_FRONT_LUA = """
local key = KEYS[1]
local idx = tonumber(ARGV[1])
local marker = ARGV[2]
local len = redis.call('llen', key)
if idx < 0 or idx >= len then
    return -1
end
local item = redis.call('lindex', key, idx)
redis.call('lset', key, idx, marker)
redis.call('lrem', key, 1, marker)
redis.call('lpush', key, item)
return 0
"""

_QUEUE_POP_RANDOM_LUA = """
local key = KEYS[1]
local selector = tonumber(ARGV[1])
local marker = ARGV[2]
local len = redis.call('llen', key)
if len <= 0 or not selector or selector < 0 or selector >= 1 then
    return false
end
local idx = math.floor(selector * len)
local item = redis.call('lindex', key, idx)
redis.call('lset', key, idx, marker)
redis.call('lrem', key, 1, marker)
return item
"""

_ENQUEUE_SONG_AND_NOTIFY_LUA = """
local function key_type(key)
    local result = redis.call('type', key)
    if type(result) == 'table' then
        return result['ok']
    end
    return result
end

local queue_type = key_type(KEYS[1])
local commands_type = key_type(KEYS[2])
if queue_type ~= 'none' and queue_type ~= 'list' then
    return redis.error_reply('WRONGTYPE song queue key must contain a list')
end
if commands_type ~= 'none' and commands_type ~= 'list' then
    return redis.error_reply('WRONGTYPE web commands key must contain a list')
end

local decoded_ok, command = pcall(cjson.decode, ARGV[2])
if not decoded_ok or type(command) ~= 'table' or type(command['payload']) ~= 'table' then
    return redis.error_reply('ERR invalid web notification template')
end

local position = redis.call('llen', KEYS[1]) + 1
command['payload']['position'] = position
local encoded_command = cjson.encode(command)
local inserted_position = redis.call('rpush', KEYS[1], ARGV[1])
redis.call('rpush', KEYS[2], encoded_command)
return inserted_position
"""


class _InMemoryRedis:
    """
    简易的内存版 Redis，用于 Redis 无法连接时的降级。
    只实现当前项目用到的最小方法集合。

    补不补一个方法的判据：**能不能给出无歧义的对等语义**。
    - 能：``pipeline``（顺序执行即可，本项目没有依赖原子性的用法）、
      ``scan``（单趟 fnmatch）、多键 ``delete``（返回删除计数）—— 都补。
    - 不能：``eval``（要跑 Lua）—— 不补；原子队列操作通过明确的内存后端
      契约分派。``expire`` 的 TTL 语义与 ``set(ex=)`` 重叠，保留最小接口。
    照这个判据加方法，不要看到缺什么就补什么，也别把已有的能力探测删掉。
    """

    def __init__(self):
        self._kv: dict[str, object] = {}
        self._lists: dict[str, list[object]] = {}
        self._expires_at: dict[str, float] = {}
        self._condition = threading.Condition()

    # --- Redis 子集 ---
    def ping(self) -> bool:
        return True

    def _get_list(self, key: str) -> list[object]:
        return self._lists.setdefault(key, [])

    def _is_expired(self, key: str) -> bool:
        expires_at = self._expires_at.get(key)
        if expires_at is None:
            return False
        if time.time() < expires_at:
            return False
        self._kv.pop(key, None)
        self._lists.pop(key, None)
        self._expires_at.pop(key, None)
        return True

    # 列表操作
    def rpush(self, key: str, *values: object) -> int:
        with self._condition:
            items = self._get_list(key)
            items.extend(values)
            self._condition.notify_all()
            return len(items)

    def lpush(self, key: str, *values: object) -> int:
        with self._condition:
            items = self._get_list(key)
            for value in values:
                items.insert(0, value)
            self._condition.notify_all()
            return len(items)

    def lrange(self, key: str, start: int, end: int) -> list[object]:
        with self._condition:
            lst = self._lists.get(key, [])
            if end == -1:
                end = len(lst) - 1
            return list(lst[start : end + 1]) if lst else []

    def llen(self, key: str) -> int:
        with self._condition:
            return len(self._lists.get(key, []))

    def lpop(self, key: str) -> object | None:
        with self._condition:
            lst = self._lists.get(key, [])
            if not lst:
                return None
            return lst.pop(0)

    def lindex(self, key: str, index: int) -> object | None:
        with self._condition:
            lst = self._lists.get(key, [])
            try:
                return lst[index]
            except IndexError:
                return None

    def lset(self, key: str, index: int, value: object) -> None:
        with self._condition:
            lst = self._get_list(key)
            if index < 0 or index >= len(lst):
                raise IndexError("list index out of range")
            lst[index] = value

    def lrem(self, key: str, count: int, value: object) -> int:
        with self._condition:
            lst = self._lists.get(key, [])
            if not lst:
                return 0
            removed = 0
            if count > 0:
                new = []
                for item in lst:
                    if removed < count and item == value:
                        removed += 1
                        continue
                    new.append(item)
                self._lists[key] = new
            elif count < 0:
                new = []
                for item in reversed(lst):
                    if removed < -count and item == value:
                        removed += 1
                        continue
                    new.append(item)
                self._lists[key] = list(reversed(new))
            else:
                new = [item for item in lst if item != value]
                removed = len(lst) - len(new)
                self._lists[key] = new
            return removed

    def queue_remove_at(self, key: str, index: int) -> bool:
        """在一个临界区内删除指定位置，语义与 Redis Lua 后端一致。"""
        with self._condition:
            items = self._lists.get(key, [])
            if index < 0 or index >= len(items):
                return False
            items.pop(index)
            return True

    def queue_move_to_front(self, key: str, index: int) -> bool:
        """在一个临界区内把指定元素移到队首。"""
        with self._condition:
            items = self._lists.get(key, [])
            if index < 0 or index >= len(items):
                return False
            item = items.pop(index)
            items.insert(0, item)
            self._condition.notify_all()
            return True

    def queue_pop_random(self, key: str) -> object | None:
        """在一个临界区内随机弹出一个列表元素。"""
        with self._condition:
            items = self._lists.get(key, [])
            if not items:
                return None
            return items.pop(random.randrange(len(items)))

    def enqueue_song_and_notify(
        self,
        queue_key: str,
        song: object,
        commands_key: str,
        notification_template: str,
    ) -> int:
        """原子入队并提交与位置一致的 Web 通知。"""
        if queue_key == commands_key:
            raise ValueError("歌曲队列与 Web 命令队列不能共用键")
        try:
            command = json.loads(notification_template)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("Web 通知模板必须是有效 JSON") from exc
        if not isinstance(command, dict) or not isinstance(command.get("payload"), dict):
            raise ValueError("Web 通知模板必须包含 payload 对象")

        with self._condition:
            self._is_expired(queue_key)
            self._is_expired(commands_key)
            # Redis 脚本会在任何写入前校验两个键的类型；内存后端
            # 必须保持相同的“全成功或全失败”边界。
            if queue_key in self._kv:
                raise TypeError("歌曲队列键必须包含列表")
            if commands_key in self._kv:
                raise TypeError("Web 命令键必须包含列表")

            queue = self._lists.setdefault(queue_key, [])
            commands = self._lists.setdefault(commands_key, [])
            position = len(queue) + 1
            payload = command["payload"]
            assert isinstance(payload, dict)
            payload["position"] = position
            encoded_command = json.dumps(
                command,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            queue.append(song)
            commands.append(encoded_command)
            self._condition.notify_all()
            return position

    def set_max_float(self, key: str, value: float) -> float:
        """原子保存有限正浮点数的最大值，并返回最终值。"""
        if not isinstance(value, (int, float)) or not (0 < float(value) < float("inf")):
            raise ValueError("value must be a finite positive number")
        candidate = float(value)
        with self._condition:
            if self._is_expired(key):
                current = 0.0
            else:
                try:
                    current = float(str(self._kv.get(key, 0.0)))
                except (TypeError, ValueError):
                    current = 0.0
                if not math.isfinite(current) or current <= 0:
                    current = 0.0
            final = max(current, candidate)
            self._kv[key] = str(final)
            self._expires_at.pop(key, None)
            return final

    # 字符串 / 通用键
    def set(
        self,
        key: str,
        value: object,
        ex: int | None = None,
        px: int | None = None,
        **kwargs: object,
    ) -> None:
        with self._condition:
            self._kv[key] = value
            if px is not None:
                self._expires_at[key] = time.time() + (float(px) / 1000.0)
            elif ex is not None:
                self._expires_at[key] = time.time() + float(ex)
            else:
                self._expires_at.pop(key, None)

    def get(self, key: str) -> object | None:
        with self._condition:
            if self._is_expired(key):
                return None
            return self._kv.get(key)

    def delete(self, *keys: str) -> int:
        """删除若干键，返回实际删掉的个数（与 redis-py 一致）。

        conversation_memory 用的是 ``delete(*keys)`` 并累加返回值，单键签名
        且返回 None 会让它在降级期直接抛 TypeError（被 try/except 吞成静默失败）。
        """
        removed = 0
        with self._condition:
            for key in keys:
                existed = key in self._kv or key in self._lists
                self._kv.pop(key, None)
                self._lists.pop(key, None)
                self._expires_at.pop(key, None)
                if existed:
                    removed += 1
        return removed

    def keys(self, pattern: str = "*") -> list[str]:
        with self._condition:
            names = set(self._kv) | set(self._lists)
        return [k for k in names if fnmatch.fnmatchcase(k, pattern)]

    def scan(
        self,
        cursor: int = 0,
        match: str | None = None,
        count: int | None = None,
    ) -> tuple[int, list[str]]:
        """单趟返回全部匹配键，游标恒为 0（表示已遍历完）。

        内存实现没有分批的必要；调用方的 ``while cursor != 0`` 循环会正常退出。
        ``match`` 走 fnmatch，与 Redis 的 glob 语义在本项目用到的范围内一致。
        """
        return 0, self.keys(match or "*")

    def pipeline(self, transaction: bool = False) -> RedisPipeline:
        """返回顺序执行的管道。

        不提供原子性 —— 本项目的 pipeline 用法都是「攒一批读/写少跑几趟网络」，
        没有依赖 MULTI/EXEC 的地方。
        """
        return _InMemoryPipeline(self)

    def blpop(self, key: str, timeout: int = 0) -> tuple[object, object] | None:
        """阻塞弹出：使用 Condition 等待，避免 CPU 空转。"""
        deadline = time.monotonic() + max(timeout, 0)
        with self._condition:
            while True:
                lst = self._lists.get(key, [])
                if lst:
                    return key, lst.pop(0)
                if timeout <= 0:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining)


class _InMemoryPipeline:
    """把调用攒起来，execute() 时按顺序在底层 _InMemoryRedis 上重放。"""

    def __init__(self, client: _InMemoryRedis):
        self._client = client
        self._queued: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _record(self, name: str, *args: object, **kwargs: object) -> _InMemoryPipeline:
        self._queued.append((name, args, kwargs))
        return self

    def get(self, key: str) -> _InMemoryPipeline:
        return self._record("get", key)

    def set(self, key: str, value: object, **kwargs: object) -> _InMemoryPipeline:
        return self._record("set", key, value, **kwargs)

    def rpush(self, key: str, *values: object) -> _InMemoryPipeline:
        return self._record("rpush", key, *values)

    def llen(self, key: str) -> _InMemoryPipeline:
        return self._record("llen", key)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def _record(*args, **kwargs):
            return self._record(name, *args, **kwargs)

        return _record

    def execute(self) -> list[object]:
        queued, self._queued = self._queued, []
        return [getattr(self._client, name)(*args, **kwargs) for name, args, kwargs in queued]

    def reset(self) -> None:
        self._queued = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.reset()
        return False


class QueueManager:
    """基于 Redis 的播放队列管理器（Redis 不可用时自动回退到内存队列）。
    支持域隔离：传入 area 后 Redis 键自动加域前缀。"""

    def __init__(self, area: str):
        normalized_area = str(area or "").strip()
        if not normalized_area:
            raise ValueError("播放域不能为空")
        self._redis: RedisDataStore = get_redis_client()
        self._area = normalized_area

    @property
    def area(self) -> str:
        return self._area

    @property
    def redis(self) -> RedisDataStore:
        # 每次访问都对齐全局客户端：内存降级恢复为真实 Redis 后，
        # 已创建的 QueueManager 实例也能自动切回。
        client = get_redis_client()
        if client is not self._redis:
            self._redis = client
        return self._redis

    def _qkey(self) -> str:
        return _area_key(KEY_QUEUE, self._area)

    def _ckey(self) -> str:
        return _area_key(KEY_CURRENT, self._area)

    def _dkey(self) -> str:
        return _area_key(KEY_DEFAULT_CHANNEL, self._area)

    def _pskey(self) -> str:
        return _area_key(KEY_PLAY_STATE, self._area)

    def _pmkey(self) -> str:
        return _area_key(KEY_PLAY_MODE, self._area)

    # ------------------------------------------------------------------
    # 队列操作
    # ------------------------------------------------------------------

    def add_to_queue(self, song_data: dict) -> int:
        """添加歌曲到队列尾部，返回队列中的位置（0-based）"""
        r = self.redis
        key = self._qkey()
        length = redis_int(
            r.rpush(key, json.dumps(song_data, ensure_ascii=False)),
            field="队列 RPUSH 返回值",
        )
        pos = length - 1
        logger.info(f"添加到队列: {song_data.get('name')} (位置 {pos})")
        return pos

    def play_next(self) -> dict | None:
        """从队列头取出下一首"""
        data = self.redis.lpop(self._qkey())
        if data:
            song = redis_json_object(data, field="队首歌曲")
            logger.info(f"队列弹出: {song.get('name')}")
            return song
        return None

    def peek_next(self) -> dict | None:
        """查看队首下一首（不弹出），用于预加载"""
        data = self.redis.lindex(self._qkey(), 0)
        if data:
            return redis_json_object(data, field="队首歌曲")
        return None

    def get_queue(self, start: int = 0, end: int = -1) -> list:
        """获取队列列表"""
        items = self.redis.lrange(self._qkey(), start, end)
        return [redis_json_object(item, field="队列歌曲") for item in items]

    def get_queue_length(self) -> int:
        return self.redis.llen(self._qkey())

    def clear_queue(self):
        """清空队列"""
        self.redis.delete(self._qkey())
        logger.info("队列已清空")

    def remove_from_queue(self, index: int) -> bool:
        """移除队列中指定位置的歌曲"""
        try:
            removed = atomic_queue_remove_at(self.redis, self._qkey(), index)
            if not removed:
                return False
            logger.info(f"移除队列位置 {index}")
            return True
        except Exception as e:
            logger.warning("移除队列位置 %d 失败: %s", index, e)
            return False

    def pop_random(self) -> dict | None:
        """原子随机弹出队列中的一首（用于随机播放模式）。"""
        try:
            data = atomic_queue_pop_random(self.redis, self._qkey())
            if not data:
                return None
            song = redis_json_object(data, field="随机队列歌曲")
            logger.info(f"队列随机弹出: {song.get('name')}")
            return song
        except Exception as e:
            logger.warning("随机弹出队列失败: %s", e)
            return None

    # ------------------------------------------------------------------
    # 当前播放
    # ------------------------------------------------------------------

    def set_current(self, song_data: dict):
        """设置当前播放歌曲"""
        self.redis.set(self._ckey(), json.dumps(song_data, ensure_ascii=False))

    def get_current(self) -> dict | None:
        """获取当前播放歌曲"""
        data = self.redis.get(self._ckey())
        if data:
            return redis_json_object(data, field="当前播放歌曲")
        return None

    def clear_current(self):
        """清除当前播放"""
        self.redis.delete(self._ckey())

    # ------------------------------------------------------------------
    # 默认频道
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 播放状态（域隔离）
    # ------------------------------------------------------------------

    def set_play_state(self, state: dict):
        self.redis.set(self._pskey(), json.dumps(state))

    def get_play_state(self) -> dict | None:
        raw = self.redis.get(self._pskey())
        return redis_json_object(raw, field="播放状态") if raw else None

    def clear_play_state(self):
        self.redis.delete(self._pskey())

    # ------------------------------------------------------------------
    # 播放模式（域隔离）
    # ------------------------------------------------------------------

    def get_play_mode(self) -> str | None:
        val = self.redis.get(self._pmkey())
        return redis_optional_text(val, field="播放模式") or None

    def set_play_mode(self, mode: str) -> None:
        self.redis.set(self._pmkey(), mode)

    # ------------------------------------------------------------------
    # 默认频道
    # ------------------------------------------------------------------

    def set_default_channel(self, channel: str):
        self.redis.set(self._dkey(), channel)

    def get_default_channel(self) -> str | None:
        val = self.redis.get(self._dkey())
        return redis_optional_text(val, field="默认频道")


def _unique_queue_marker() -> str:
    """生成无法与正常 JSON 队列项混淆的临时标记。"""
    return f"__oopz_queue_marker__:{uuid.uuid4().hex}"


def atomic_queue_remove_at(redis_client: object, key: str, index: int) -> bool:
    """通过已知后端契约原子删除列表位置。"""
    if isinstance(redis_client, _InMemoryRedis):
        return redis_client.queue_remove_at(key, index)
    result = cast(RedisScriptExecutor, redis_client).eval(
        _QUEUE_REMOVE_AT_LUA,
        1,
        key,
        int(index),
        _unique_queue_marker(),
    )
    return redis_int(result, field="队列删除脚本返回值") == 0


def atomic_queue_move_to_front(redis_client: object, key: str, index: int) -> bool:
    """通过已知后端契约原子地把列表位置移到队首。"""
    if isinstance(redis_client, _InMemoryRedis):
        return redis_client.queue_move_to_front(key, index)
    result = cast(RedisScriptExecutor, redis_client).eval(
        _QUEUE_MOVE_TO_FRONT_LUA,
        1,
        key,
        int(index),
        _unique_queue_marker(),
    )
    return redis_int(result, field="队列置顶脚本返回值") == 0


def atomic_queue_pop_random(redis_client: object, key: str) -> object | None:
    """通过已知后端契约原子随机弹出列表元素。"""
    if isinstance(redis_client, _InMemoryRedis):
        return redis_client.queue_pop_random(key)
    return cast(RedisScriptExecutor, redis_client).eval(
        _QUEUE_POP_RANDOM_LUA,
        1,
        key,
        random.random(),
        _unique_queue_marker(),
    )


def atomic_enqueue_song_and_notify(
    redis_client: object,
    queue_key: str,
    song: object,
    commands_key: str,
    notification_template: str,
) -> int:
    """原子入队、返回 1-based 位置并按同一顺序写通知。"""
    if isinstance(redis_client, _InMemoryRedis):
        return redis_client.enqueue_song_and_notify(
            queue_key,
            song,
            commands_key,
            notification_template,
        )
    result = cast(RedisScriptExecutor, redis_client).eval(
        _ENQUEUE_SONG_AND_NOTIFY_LUA,
        2,
        queue_key,
        commands_key,
        song,
        notification_template,
    )
    return redis_int(result, field="点歌入队脚本返回值")


def _try_connect_redis() -> RedisDataStore | None:
    """尝试建立真实 Redis 连接，失败返回 None。"""
    client = None
    try:
        options = dict(REDIS_CONFIG)
        # redis-py 用 None 表示无超时；配置显式写 None 也不得关掉
        # 恢复状态机的网络上界。
        if options.get("socket_connect_timeout") is None:
            options["socket_connect_timeout"] = 3.0
        if options.get("socket_timeout") is None:
            options["socket_timeout"] = 5.0
        options.setdefault("health_check_interval", 30)
        client = redis.Redis(**options)
        client.ping()
        return cast(RedisDataStore, client)
    except Exception as e:
        logger.debug(f"Redis 连接尝试失败: {e}")
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.debug("关闭失败的 Redis 探测连接失败", exc_info=True)
        return None


def is_degraded() -> bool:
    """当前是否处于内存降级状态。

    ``_InMemoryRedis.ping()`` 恒返回 True，光靠 ping 判断的话 Redis 完全挂掉时
    /health 也是一片绿、容器还被判成健康。需要区分时问这个函数。
    """
    return isinstance(_redis_client, _InMemoryRedis)


def _close_retired_redis_client(
    client: RedisDataStore,
    *,
    retirement_generation: int,
) -> None:
    """在身份与 generation 复核后关闭已退役的真实 Redis 客户端。"""
    with _redis_condition:
        if (
            _redis_generation < retirement_generation
            or _redis_client is client
            or _redis_probe_client is client
        ):
            return
    try:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    except Exception:
        logger.debug("关闭已退役 Redis 客户端失败", exc_info=True)


def get_redis_client(force_reset: bool = False) -> RedisDataStore:
    """返回全局共享 Redis 客户端；连接失败时统一回退到内存实现。

    真实客户端会在交付前于全局锁外执行健康探测，运行期间断线时原子切入
    内存实现；探测完成前已持有旧客户端的命令仍按底层 Redis 的结果显式成败，
    不自动重放非幂等写入。

    内存降级不是永久的：之后每隔 _REDIS_RETRY_INTERVAL 秒在访问时探测一次
    真实 Redis，恢复后自动切回（内存实现中的临时数据不迁移，播放队列等
    会从 Redis 中的持久数据重新开始）。
    """
    global _redis_client, _last_redis_retry, _redis_generation
    global _redis_probe_in_flight, _redis_probe_token, _redis_probe_client
    probe_kind = ""
    probe_generation = 0
    probe_client: RedisDataStore | None = None
    probe_token: object | None = None
    reset_pending = force_reset
    waiting_token: object | None = None
    wait_deadline = 0.0
    retired_clients: list[tuple[RedisDataStore, int]] = []

    while True:
        immediate_client: RedisDataStore | None = None
        with _redis_condition:
            client_replaced = False
            if reset_pending:
                previous_client = _redis_client
                previous_is_probed = (
                    _redis_probe_token is not None
                    and _redis_probe_client is previous_client
                )
                _redis_generation += 1
                _redis_client = _InMemoryRedis()
                _last_redis_retry = 0.0
                reset_pending = False
                client_replaced = True
                if (
                    previous_client is not None
                    and not isinstance(previous_client, _InMemoryRedis)
                    and not previous_is_probed
                ):
                    retired_clients.append((previous_client, _redis_generation))
            if _redis_client is None:
                _redis_generation += 1
                _redis_client = _InMemoryRedis()
                _last_redis_retry = 0.0
                client_replaced = True

            if _redis_client is None:
                raise RuntimeError("Redis 客户端状态异常")
            active_probe_token = _redis_probe_token
            if active_probe_token is not None:
                if isinstance(_redis_client, _InMemoryRedis) and not client_replaced:
                    # 恢复探测成功后该 fallback 就会退役。等待探测者公布
                    # 结果，避免把旧实例交给随后的写方。Condition.wait()
                    # 会释放全局锁，探测者因此能够完成并唤醒所有等待者。
                    now = time.monotonic()
                    if waiting_token is not active_probe_token:
                        waiting_token = active_probe_token
                        wait_deadline = now + _REDIS_PROBE_WAIT_TIMEOUT
                    remaining = wait_deadline - now
                    if remaining > 0:
                        _redis_condition.wait(timeout=remaining)
                        continue

                    # 探测超时：保留当前 fallback 及其已有写入，但让旧令牌
                    # 立即失效。旧 candidate 之后即使连接成功，也只能被关闭，
                    # 不得替换这个 fallback，否则超时后的写入会丢失。
                    if _redis_probe_token is active_probe_token:
                        _redis_generation += 1
                        _redis_probe_token = None
                        _redis_probe_client = None
                        _redis_probe_in_flight = False
                        _last_redis_retry = now
                        _redis_condition.notify_all()
                    immediate_client = _redis_client
                # 真实 Redis 的健康探测期间仍交付同一真实客户端。
                # force_reset 创建的新 fallback 也不属于当前陈旧探测。
                if immediate_client is None:
                    immediate_client = _redis_client
            else:
                now = time.monotonic()
                if isinstance(_redis_client, _InMemoryRedis):
                    if now - _last_redis_retry < _REDIS_RETRY_INTERVAL:
                        immediate_client = _redis_client
                    else:
                        _last_redis_retry = now
                        probe_kind = "recovery"
                else:
                    # redis-py 会重连单条命令，但若服务持续不可用，原来的全局客户端
                    # 永远不会进入内存降级。每次交付真实客户端前做一次轻量 PING，
                    # 网络 I/O 放在锁外；失败后由 generation/identity 校验原子切换。
                    probe_kind = "health"

                if immediate_client is None:
                    probe_token = object()
                    _redis_probe_token = probe_token
                    _redis_probe_in_flight = True
                    probe_generation = _redis_generation
                    probe_client = _redis_client
                    _redis_probe_client = probe_client

        for retired_client, retirement_generation in retired_clients:
            _close_retired_redis_client(
                retired_client,
                retirement_generation=retirement_generation,
            )
        retired_clients.clear()
        if immediate_client is not None:
            return immediate_client
        if probe_token is not None:
            break

    # 网络连接和 PING 可能阻塞，绝不能占用全局状态锁。健康探测期间
    # 调用者可继续共享真实客户端；恢复探测期间的 fallback 调用者则通过
    # Condition 等待最终选定的客户端。
    candidate: RedisDataStore | None = None
    healthy = False
    fatal_probe_error: BaseException | None = None
    if probe_kind == "recovery":
        try:
            candidate = _try_connect_redis()
        except Exception:
            # _try_connect_redis 自身会吸收常规网络错误；这层兜底保证
            # 测试替身或意外实现异常也不会让等待者永久阻塞。
            logger.exception("Redis 恢复探测异常")
        except BaseException as exc:
            fatal_probe_error = exc
    elif probe_client is not None:
        try:
            healthy = bool(probe_client.ping())
        except Exception as e:
            logger.warning("Redis 运行时连接已中断，切换到内存队列: %s", e)
        except BaseException as exc:
            fatal_probe_error = exc

    discarded_client: RedisDataStore | None = None
    with _redis_condition:
        probe_owns_state = probe_token is not None and _redis_probe_token is probe_token
        current_generation = _redis_generation
        probe_is_current = (
            probe_owns_state
            and current_generation == probe_generation
            and _redis_client is probe_client
        )
        if probe_kind == "recovery" and probe_is_current:
            if candidate is not None:
                logger.info("Redis 连接成功，从内存队列切回 Redis")
                _redis_client = candidate
                _redis_generation += 1
            else:
                logger.error(
                    "Redis 连接失败，将使用内存队列（每 %.0fs 自动重试）",
                    _REDIS_RETRY_INTERVAL,
                )
        elif (
            probe_kind == "recovery"
            and candidate is not None
            and candidate is not _redis_client
        ):
            discarded_client = candidate
        elif probe_kind == "health" and probe_is_current and not healthy:
            discarded_client = probe_client
            _redis_client = _InMemoryRedis()
            _redis_generation += 1
            _last_redis_retry = time.monotonic()
        elif (
            probe_kind == "health"
            and probe_client is not None
            and probe_client is not _redis_client
        ):
            discarded_client = probe_client
        if probe_owns_state:
            _redis_probe_token = None
            _redis_probe_client = None
            _redis_probe_in_flight = False
            _redis_condition.notify_all()
        selected_client = _redis_client

    if discarded_client is not None:
        _close_retired_redis_client(
            discarded_client,
            retirement_generation=current_generation,
        )
    if selected_client is None:
        raise RuntimeError("Redis 客户端状态异常")
    if fatal_probe_error is not None:
        raise fatal_probe_error
    if not probe_owns_state:
        # 本 probe 在网络 I/O 期间已超时或被新一代状态取代。收尾后
        # 重新选择，不能把另一个正在探测的 fallback 直接交给调用方。
        return get_redis_client()
    return selected_client
