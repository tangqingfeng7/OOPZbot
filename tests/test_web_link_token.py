import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import web.web_link_token as web_link_token
from web.web_link_token import (
    KEY_WEB_LAST_ACCESS,
    clear_token,
    seconds_since_access,
    set_token,
    touch_access,
)


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


class LastAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        # 模块级内存回退在用例间共享，逐个重置
        web_link_token._memory_token = ""
        web_link_token._memory_last_access = 0.0
        self.redis = _FakeRedis()

    def test_never_accessed_is_infinite(self) -> None:
        self.assertEqual(seconds_since_access(redis_client=self.redis), float("inf"))

    def test_touch_makes_elapsed_small(self) -> None:
        touch_access(redis_client=self.redis)
        self.assertLess(seconds_since_access(redis_client=self.redis), 5)

    def test_touch_is_persisted_to_redis(self) -> None:
        touch_access(redis_client=self.redis)
        self.assertIn(KEY_WEB_LAST_ACCESS, self.redis.store)

    def test_falls_back_to_memory_when_redis_has_no_value(self) -> None:
        touch_access(redis_client=None)  # 只写内存
        self.assertLess(seconds_since_access(redis_client=self.redis), 5)

    def test_clear_token_resets_last_access(self) -> None:
        touch_access(redis_client=self.redis)
        clear_token(redis_client=self.redis)

        self.assertEqual(seconds_since_access(redis_client=self.redis), float("inf"))
        self.assertNotIn(KEY_WEB_LAST_ACCESS, self.redis.store)

    def test_stale_access_reports_elapsed_time(self) -> None:
        self.redis.store[KEY_WEB_LAST_ACCESS] = str(time.time() - 3600)
        elapsed = seconds_since_access(redis_client=self.redis)

        self.assertGreater(elapsed, 3000)
        self.assertLess(elapsed, 4200)

    def test_corrupt_value_falls_back_to_memory(self) -> None:
        self.redis.store[KEY_WEB_LAST_ACCESS] = "not-a-number"
        self.assertEqual(seconds_since_access(redis_client=self.redis), float("inf"))


class IdleReleaseGuardTest(unittest.TestCase):
    """空闲释放必须同时满足「队列空」与「播放器无人使用」。"""

    def setUp(self) -> None:
        web_link_token._memory_token = ""
        web_link_token._memory_last_access = 0.0
        self.redis = _FakeRedis()

    def test_active_player_blocks_release(self) -> None:
        set_token("tok", redis_client=self.redis)
        touch_access(redis_client=self.redis)

        # 队列空闲 1800s，但播放器刚刚被访问过 → 不应释放
        timeout = 1800
        self.assertLess(seconds_since_access(redis_client=self.redis), timeout)

    def test_idle_player_allows_release(self) -> None:
        set_token("tok", redis_client=self.redis)
        self.redis.store[KEY_WEB_LAST_ACCESS] = str(time.time() - 3600)

        timeout = 1800
        self.assertGreaterEqual(seconds_since_access(redis_client=self.redis), timeout)


if __name__ == "__main__":
    unittest.main()
