"""后台按域分槽的缓存
"""

import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from web.admin.shared import TtlCache  # noqa: E402


class TtlCacheTest(unittest.TestCase):
    def test_switching_back_and_forth_still_hits(self) -> None:
        """核心回归：A→B→A 时 A 必须仍在缓存里。"""
        cache = TtlCache(ttl=60)
        cache.set("area-A", {"members": 1})
        cache.set("area-B", {"members": 2})

        self.assertEqual(cache.get("area-A"), {"members": 1})
        self.assertEqual(cache.get("area-B"), {"members": 2})

    def test_expired_entry_is_dropped(self) -> None:
        cache = TtlCache(ttl=0.05)
        cache.set("area-A", "旧值")
        time.sleep(0.08)
        self.assertIsNone(cache.get("area-A"))

    def test_unknown_key_returns_none(self) -> None:
        self.assertIsNone(TtlCache(ttl=60).get("从未写入"))

    def test_least_recently_used_is_evicted_when_full(self) -> None:
        """域很多时不能无限增长。"""
        cache = TtlCache(ttl=60, maxsize=2)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.get("a")          # a 变为最近使用
        cache.set("c", 3)       # 淘汰最久未用的 b

        self.assertEqual(cache.get("a"), 1)
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("c"), 3)
        self.assertEqual(len(cache), 2)

    def test_invalidate_single_key_keeps_others(self) -> None:
        """管理操作只应让被改动的那个域失效，不该清空全部。"""
        cache = TtlCache(ttl=60)
        cache.set("area-A", 1)
        cache.set("area-B", 2)

        cache.invalidate("area-A")

        self.assertIsNone(cache.get("area-A"))
        self.assertEqual(cache.get("area-B"), 2)

    def test_invalidate_all(self) -> None:
        cache = TtlCache(ttl=60)
        cache.set("area-A", 1)
        cache.set("area-B", 2)

        cache.invalidate()

        self.assertEqual(len(cache), 0)

    def test_overwriting_a_key_refreshes_it(self) -> None:
        cache = TtlCache(ttl=60)
        cache.set("area-A", "旧")
        cache.set("area-A", "新")
        self.assertEqual(cache.get("area-A"), "新")
        self.assertEqual(len(cache), 1)


class AdminCachesArePerAreaTest(unittest.TestCase):
    """三个后台缓存都必须是按域分槽的，不能再退回单槽字典。"""

    def test_member_channel_and_area_meta_caches_are_ttl_caches(self) -> None:
        from web.admin.members import _channels, _members
        from web.admin.shared import _area

        for cache, name in (
            (_area._members_resp_cache, "成员列表"),
            (_channels._channels_cache, "频道列表"),
            (_members._area_meta_cache, "域信息"),
        ):
            with self.subTest(cache=name):
                self.assertIsInstance(cache, TtlCache)


if __name__ == "__main__":
    unittest.main()
