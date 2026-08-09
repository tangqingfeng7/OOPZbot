import math
import secrets
import threading
import time

from core.logger_config import get_logger

logger = get_logger("WebLinkToken")

KEY_WEB_ACCESS_TOKEN = "music:web_access_token"
KEY_WEB_ACTIVE_AREA = "music:web_active_area"
KEY_WEB_LAST_ACCESS = "music:web_last_access"

_lock = threading.Lock()
_memory_token: str = ""
_memory_area: str = ""
_memory_last_access: float = 0.0

_MAX_TIMESTAMP_LUA = """
local candidate = tonumber(ARGV[1])
if not candidate or candidate <= 0 or candidate ~= candidate or candidate == math.huge then
    return redis.error_reply('candidate must be a finite positive timestamp')
end
local current = tonumber(redis.call('get', KEYS[1]))
if not current or current <= 0 or current ~= current or current == math.huge then
    current = 0
end
if candidate > current then
    redis.call('set', KEYS[1], ARGV[1])
    return candidate
end
return current
"""


def _positive_finite_timestamp(value) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 and math.isfinite(parsed) else 0.0


def _normalize_ttl(ttl_seconds=None) -> int:
    """标准化 TTL 秒数。<=0 表示不设置过期。"""
    try:
        ttl = int(ttl_seconds or 0)
    except (TypeError, ValueError):
        return 0
    return ttl if ttl > 0 else 0


def get_token(redis_client=None) -> str:
    """读取当前访问令牌。"""
    global _memory_token
    token = ""
    redis_read_ok = False
    if redis_client is not None:
        try:
            raw = redis_client.get(KEY_WEB_ACCESS_TOKEN)
            redis_read_ok = True
            if isinstance(raw, bytes):
                token = raw.decode("utf-8", errors="ignore")
            elif isinstance(raw, str):
                token = raw
        except Exception as e:
            logger.debug(f"Redis 读取 Web 令牌失败，使用内存回退: {e}")
    if token:
        with _lock:
            _memory_token = token
        return token
    if redis_client is not None and redis_read_ok:
        with _lock:
            _memory_token = ""
        return ""
    with _lock:
        return _memory_token


def set_token(token: str, redis_client=None, ttl_seconds=None):
    """设置访问令牌。"""
    global _memory_token
    val = token or ""
    with _lock:
        _memory_token = val
    if redis_client is not None:
        ttl = _normalize_ttl(ttl_seconds)
        try:
            if ttl > 0:
                try:
                    redis_client.set(KEY_WEB_ACCESS_TOKEN, val, ex=ttl)
                except TypeError:
                    redis_client.set(KEY_WEB_ACCESS_TOKEN, val)
                    if hasattr(redis_client, "expire"):
                        redis_client.expire(KEY_WEB_ACCESS_TOKEN, ttl)
            else:
                redis_client.set(KEY_WEB_ACCESS_TOKEN, val)
        except Exception as e:
            logger.debug(f"Redis 写入 Web 令牌失败，已仅写内存: {e}")


def ensure_token(redis_client=None, ttl_seconds=None) -> str:
    """确保存在可用令牌，不存在则生成。"""
    ttl = _normalize_ttl(ttl_seconds)
    token = get_token(redis_client=redis_client)
    if token:
        # 有效令牌存在时，按需刷新 Redis 过期时间（滑动续期）
        if redis_client is not None and ttl > 0:
            set_token(token, redis_client=redis_client, ttl_seconds=ttl)
        return token
    token = secrets.token_urlsafe(18)
    set_token(token, redis_client=redis_client, ttl_seconds=ttl)
    return token


def clear_token(redis_client=None):
    """清理访问令牌。"""
    global _memory_token, _memory_last_access
    with _lock:
        _memory_token = ""
        _memory_last_access = 0.0
    if redis_client is not None:
        try:
            redis_client.delete(KEY_WEB_ACCESS_TOKEN)
        except Exception as e:
            logger.debug(f"Redis 清理 Web 令牌失败: {e}")
        try:
            redis_client.delete(KEY_WEB_LAST_ACCESS)
        except Exception as e:
            logger.debug(f"Redis 清理 Web 访问时间失败: {e}")


def touch_access(redis_client=None):
    """记录播放器最近一次被使用的时间。

    空闲释放只看播放队列是否为空，但用户可能正开着页面搜歌、翻喜欢列表 ——
    那些请求不进队列。没有这个时间戳，活跃用户会在队列空 30 分钟后被踢下线。
    """
    global _memory_last_access
    now = _positive_finite_timestamp(time.time())
    if now <= 0:
        return
    with _lock:
        current = _positive_finite_timestamp(_memory_last_access)
        _memory_last_access = max(current, now)
    if redis_client is not None:
        try:
            if hasattr(redis_client, "set_max_float"):
                redis_client.set_max_float(KEY_WEB_LAST_ACCESS, now)
            elif hasattr(redis_client, "eval"):
                redis_client.eval(_MAX_TIMESTAMP_LUA, 1, KEY_WEB_LAST_ACCESS, str(now))
            else:
                # 仅供实现最小 Redis 接口的测试替身；生产 Redis 必须走 Lua。
                current = _positive_finite_timestamp(redis_client.get(KEY_WEB_LAST_ACCESS))
                if now > current:
                    redis_client.set(KEY_WEB_LAST_ACCESS, str(now))
        except Exception as e:
            logger.debug(f"Redis 记录 Web 访问时间失败: {e}")


def seconds_since_access(redis_client=None) -> float:
    """距最近一次播放器访问过去了多少秒；从未访问过返回 ``float('inf')``。"""
    redis_last = 0.0
    if redis_client is not None:
        try:
            raw = redis_client.get(KEY_WEB_LAST_ACCESS)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            redis_last = _positive_finite_timestamp(raw)
        except Exception as e:
            logger.debug(f"Redis 读取 Web 访问时间失败，使用内存回退: {e}")
    with _lock:
        memory_last = _positive_finite_timestamp(_memory_last_access)
    last = max(redis_last, memory_last)
    if last <= 0:
        return float("inf")
    return max(0.0, time.time() - last)


def get_active_area(redis_client=None) -> str:
    """读取当前 Web 播放器关联的活跃域 ID。"""
    global _memory_area
    if redis_client is not None:
        try:
            raw = redis_client.get(KEY_WEB_ACTIVE_AREA)
            val = ""
            if isinstance(raw, bytes):
                val = raw.decode("utf-8", errors="ignore")
            elif isinstance(raw, str):
                val = raw
            with _lock:
                _memory_area = val
            return val
        except Exception:
            pass
    with _lock:
        return _memory_area


def set_active_area(area: str, redis_client=None):
    """保存当前 Web 播放器关联的活跃域 ID。"""
    global _memory_area
    val = (area or "").strip()
    with _lock:
        _memory_area = val
    if redis_client is not None:
        try:
            redis_client.set(KEY_WEB_ACTIVE_AREA, val)
        except Exception as e:
            logger.debug(f"Redis 写入 active area 失败: {e}")
