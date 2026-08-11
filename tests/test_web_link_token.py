import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
# 让同目录的测试助手在 discover 与直接运行两种方式下都可导入
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from async_barrier import AsyncBarrier  # noqa: E402

import web.web_link_token as web_link_token  # noqa: E402
from web.web_link_token import (  # noqa: E402
    KEY_WEB_LAST_ACCESS,
    clear_token,
    seconds_since_access,
    set_token,
    touch_access,
)


class _FakeRedis:
    """最小 Redis 替身：命令方法均为异步，且刻意不提供 set_max_float，
    以便覆盖 touch_access 里 get/set 的回退分支。"""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)


class LastAccessTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 模块级内存回退在用例间共享，逐个重置
        web_link_token._memory_token = ""
        web_link_token._memory_last_access = 0.0
        self.redis = _FakeRedis()

    async def test_never_accessed_is_infinite(self) -> None:
        self.assertEqual(await seconds_since_access(redis_client=self.redis), float("inf"))

    async def test_touch_makes_elapsed_small(self) -> None:
        await touch_access(redis_client=self.redis)
        self.assertLess(await seconds_since_access(redis_client=self.redis), 5)

    async def test_touch_is_persisted_to_redis(self) -> None:
        await touch_access(redis_client=self.redis)
        self.assertIn(KEY_WEB_LAST_ACCESS, self.redis.store)

    async def test_falls_back_to_memory_when_redis_has_no_value(self) -> None:
        await touch_access(redis_client=None)  # 只写内存
        self.assertLess(await seconds_since_access(redis_client=self.redis), 5)

    async def test_clear_token_resets_last_access(self) -> None:
        await touch_access(redis_client=self.redis)
        await clear_token(redis_client=self.redis)

        self.assertEqual(await seconds_since_access(redis_client=self.redis), float("inf"))
        self.assertNotIn(KEY_WEB_LAST_ACCESS, self.redis.store)

    async def test_stale_access_reports_elapsed_time(self) -> None:
        self.redis.store[KEY_WEB_LAST_ACCESS] = str(time.time() - 3600)
        elapsed = await seconds_since_access(redis_client=self.redis)

        self.assertGreater(elapsed, 3000)
        self.assertLess(elapsed, 4200)

    async def test_corrupt_value_falls_back_to_memory(self) -> None:
        self.redis.store[KEY_WEB_LAST_ACCESS] = "not-a-number"
        self.assertEqual(await seconds_since_access(redis_client=self.redis), float("inf"))

    async def test_redis_stale_value_cannot_hide_newer_memory_access(self) -> None:
        now = time.time()
        web_link_token._memory_last_access = now
        self.redis.store[KEY_WEB_LAST_ACCESS] = str(now - 3600)

        self.assertLess(await seconds_since_access(redis_client=self.redis), 5)

    async def test_non_finite_values_are_ignored(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                self.redis.store[KEY_WEB_LAST_ACCESS] = value
                self.assertEqual(await seconds_since_access(redis_client=self.redis), float("inf"))

    async def test_out_of_order_touch_keeps_maximum_timestamp(self) -> None:
        from core.queue_manager import _InMemoryRedis

        redis_client = _InMemoryRedis()
        with mock.patch.object(web_link_token.time, "time", return_value=200.0):
            await touch_access(redis_client=redis_client)
        with mock.patch.object(web_link_token.time, "time", return_value=100.0):
            await touch_access(redis_client=redis_client)

        self.assertEqual(float(str(await redis_client.get(KEY_WEB_LAST_ACCESS))), 200.0)
        self.assertEqual(web_link_token._memory_last_access, 200.0)

    async def test_memory_backend_replaces_non_finite_existing_timestamp(self) -> None:
        from core.queue_manager import _InMemoryRedis

        redis_client = _InMemoryRedis()
        for poisoned in ("nan", "inf", "-inf"):
            with self.subTest(poisoned=poisoned):
                await redis_client.set(KEY_WEB_LAST_ACCESS, poisoned)
                self.assertEqual(
                    await redis_client.set_max_float(KEY_WEB_LAST_ACCESS, 200.0),
                    200.0,
                )

    async def test_process_memory_replaces_non_finite_existing_timestamp(self) -> None:
        for poisoned in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(poisoned=poisoned):
                web_link_token._memory_last_access = poisoned
                with mock.patch.object(web_link_token.time, "time", return_value=200.0):
                    await touch_access(redis_client=None)
                self.assertEqual(web_link_token._memory_last_access, 200.0)

    async def test_memory_backend_concurrent_max_update_is_atomic(self) -> None:
        from core.queue_manager import _InMemoryRedis

        redis_client = _InMemoryRedis()
        values = [50.0, 200.0, 125.0, 175.0]
        barrier = AsyncBarrier(len(values))

        async def update(value: float) -> None:
            # 四个更新同时抵达，最终值必须是最大值而非最后写入者
            await barrier.wait()
            await redis_client.set_max_float(KEY_WEB_LAST_ACCESS, value)

        await asyncio.gather(*(update(value) for value in values))

        self.assertEqual(
            float(str(await redis_client.get(KEY_WEB_LAST_ACCESS))), max(values)
        )


class IdleReleaseGuardTest(unittest.IsolatedAsyncioTestCase):
    """空闲释放必须同时满足「队列空」与「播放器无人使用」。"""

    def setUp(self) -> None:
        web_link_token._memory_token = ""
        web_link_token._memory_last_access = 0.0
        self.redis = _FakeRedis()

    async def test_active_player_blocks_release(self) -> None:
        await set_token("tok", redis_client=self.redis)
        await touch_access(redis_client=self.redis)

        # 队列空闲 1800s，但播放器刚刚被访问过 → 不应释放
        timeout = 1800
        self.assertLess(await seconds_since_access(redis_client=self.redis), timeout)

    async def test_idle_player_allows_release(self) -> None:
        await set_token("tok", redis_client=self.redis)
        self.redis.store[KEY_WEB_LAST_ACCESS] = str(time.time() - 3600)

        timeout = 1800
        self.assertGreaterEqual(await seconds_since_access(redis_client=self.redis), timeout)


if __name__ == "__main__":
    unittest.main()
