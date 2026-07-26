"""Web 层限流与登录失败锁定。

单进程内存实现：整个进程只跑一个 uvicorn worker，所有计数都在本进程内。
多实例部署时各实例各算各的，需要在反代层另行兜底。

分桶用的客户端 IP 判定也收口在这里 —— nginx 用 ``$proxy_add_x_forwarded_for``
是**追加**语义，客户端自带的 X-Forwarded-For 会排在真实地址前面，所以取首位
等于取攻击者可控的值。``X-Real-IP`` 由 nginx 从 ``$remote_addr`` 覆盖写入，
不可伪造，优先用它。
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅用于类型标注：运行时不引入 fastapi，便于独立单测
    from fastapi import Request


class RateLimiter:
    """基于滑动窗口的简易内存速率限制器。"""

    _MAX_TRACKED_IPS = 2000

    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()
        self._last_cleanup = 0.0

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._hits[key]
            bucket[:] = [t for t in bucket if t > cutoff]
            if len(bucket) >= self._max:
                return False
            bucket.append(now)
            if now - self._last_cleanup > self._window:
                self._evict_stale(cutoff)
                self._last_cleanup = now
        return True

    def _evict_stale(self, cutoff: float) -> None:
        stale = [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]
        for k in stale:
            del self._hits[k]
        if len(self._hits) > self._MAX_TRACKED_IPS:
            by_recent = sorted(self._hits.items(), key=lambda x: x[1][-1] if x[1] else 0)
            for k, _ in by_recent[: len(by_recent) - self._MAX_TRACKED_IPS]:
                del self._hits[k]

    def reset(self) -> None:
        """清空所有桶。仅供测试使用 —— 限流器是模块级单例，用例间会互相污染。"""
        with self._lock:
            self._hits.clear()
            self._last_cleanup = 0.0


class LoginGuard:
    """后台登录失败锁定：同一 IP 连续失败若干次后，锁定一段固定时长。

    只做 per-IP 固定时长锁定，不做指数退避、不做全站兜底 —— 全站兜底会给出一个
    「打满阈值让管理员登不进」的廉价 DoS 面，收益不抵风险。
    """

    _MAX_TRACKED_IPS = 2000

    def __init__(self) -> None:
        self._failures: dict[str, int] = defaultdict(int)
        self._locked_until: dict[str, float] = {}
        self._lock = Lock()

    def locked_seconds(self, key: str, lock_seconds: int) -> int:
        """还需等待多少秒；未锁定返回 0。``lock_seconds`` 为 0 表示关闭锁定。"""
        if lock_seconds <= 0:
            return 0
        with self._lock:
            until = self._locked_until.get(key, 0.0)
            remaining = until - time.monotonic()
            if remaining <= 0:
                self._locked_until.pop(key, None)
                return 0
            return int(remaining) + 1

    def record_failure(self, key: str, max_failures: int, lock_seconds: int) -> bool:
        """记一次失败；返回本次是否触发锁定。"""
        if max_failures <= 0 or lock_seconds <= 0:
            return False
        with self._lock:
            self._failures[key] += 1
            if self._failures[key] < max_failures:
                return False
            self._failures.pop(key, None)
            self._locked_until[key] = time.monotonic() + lock_seconds
            self._evict_stale()
            return True

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)

    def _evict_stale(self) -> None:
        now = time.monotonic()
        for k, until in list(self._locked_until.items()):
            if until <= now:
                del self._locked_until[k]
        if len(self._failures) > self._MAX_TRACKED_IPS:
            self._failures.clear()

    def reset(self) -> None:
        """清空计数。仅供测试使用。"""
        with self._lock:
            self._failures.clear()
            self._locked_until.clear()


def client_ip(request: "Request", trust_proxy: bool) -> str:
    """限流分桶用的客户端标识。

    ``trust_proxy`` 为真时优先 ``X-Real-IP``（nginx 从 ``$remote_addr`` 覆盖写入，
    客户端伪造不了），其次取 ``X-Forwarded-For`` 的**末位**（追加语义下末位才是
    离本机最近的那一跳），最后回落到直连地址。

    直接把 uvicorn 暴露到公网时应设为假，否则任何人都能伪造这两个头绕过限流。
    """
    if trust_proxy:
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            hops = [part.strip() for part in forwarded.split(",") if part.strip()]
            if hops:
                return hops[-1]
    return request.client.host if request.client else "unknown"


# 路径 → 限流器。未命中的走 DEFAULT_LIMITER。
SEARCH_LIMITER = RateLimiter(max_requests=15, window_seconds=60)
LOGIN_LIMITER = RateLimiter(max_requests=10, window_seconds=60)
DEFAULT_LIMITER = RateLimiter(max_requests=200, window_seconds=60)

PATH_LIMITERS: dict[str, RateLimiter] = {
    "/api/search": SEARCH_LIMITER,
    "/admin/api/login": LOGIN_LIMITER,
}

login_guard = LoginGuard()


def limiter_for(path: str) -> RateLimiter:
    return PATH_LIMITERS.get(path, DEFAULT_LIMITER)


def reset_all() -> None:
    """重置全部限流状态。仅供测试使用。"""
    for limiter in {*PATH_LIMITERS.values(), DEFAULT_LIMITER}:
        limiter.reset()
    login_guard.reset()


__all__ = [
    "RateLimiter",
    "LoginGuard",
    "client_ip",
    "limiter_for",
    "login_guard",
    "reset_all",
    "PATH_LIMITERS",
    "DEFAULT_LIMITER",
    "SEARCH_LIMITER",
    "LOGIN_LIMITER",
]
