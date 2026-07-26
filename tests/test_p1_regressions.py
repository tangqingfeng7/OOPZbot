"""P1 缺陷的回归测试（todo.md「P1 · 确定性 Bug」）。"""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from core.redis_keys import (
    CURRENT,
    PLAY_STATE,
    QUEUE,
    VOLUME,
    WEB_COMMANDS,
    area_key,
)


class AreaKeyTest(unittest.TestCase):
    """area_key 早先硬假设 base 含 ':' 且前缀为 'music'，传别的会 IndexError。"""

    def test_existing_keys_are_unchanged(self) -> None:
        # 这几条是线上实际在用的键，改法必须完全保形
        self.assertEqual(area_key(QUEUE, "A1"), "music:A1:queue")
        self.assertEqual(area_key(CURRENT, "A1"), "music:A1:current")
        self.assertEqual(area_key(PLAY_STATE, "A1"), "music:A1:play_state")
        self.assertEqual(area_key(VOLUME, "A1"), "music:A1:volume")
        self.assertEqual(area_key(WEB_COMMANDS, "A1"), "music:A1:web_commands")

    def test_empty_area_falls_back_to_global_key(self) -> None:
        self.assertEqual(area_key(QUEUE, ""), QUEUE)

    def test_base_without_colon_does_not_raise(self) -> None:
        self.assertEqual(area_key("volume", "A1"), "volume:A1")

    def test_prefix_is_taken_from_base_not_hardcoded(self) -> None:
        # 早先无论 base 是什么都拼死 "music:" 前缀
        self.assertEqual(area_key("chat:memory", "A1"), "chat:A1:memory")


class StatisticsPlatformCountTest(unittest.TestCase):
    """每日首条记录曾出现 total_plays=1 但该平台计数为 2。"""

    def test_first_write_of_the_day_counts_once(self) -> None:
        import core.database as database

        rows: dict[str, str] = {}
        captured: list[tuple] = []

        class _Conn:
            def execute(self, sql, params=()):
                captured.append((sql, params))
                if sql.strip().startswith("INSERT"):
                    rows["platform_breakdown"] = params[3]
                elif sql.strip().startswith("SELECT"):
                    return SimpleNamespace(
                        fetchone=lambda: {"platform_breakdown": rows["platform_breakdown"]}
                    )
                elif sql.strip().startswith("UPDATE"):
                    rows["platform_breakdown"] = params[0]
                return SimpleNamespace(fetchone=lambda: None)

        class _Ctx:
            def __enter__(self):
                return _Conn()

            def __exit__(self, *a):
                return False

        with mock.patch.object(database, "db_connection", lambda: _Ctx()):
            database.Statistics.update_today("netease", cache_hit=False)

        breakdown = json.loads(rows["platform_breakdown"])
        self.assertEqual(breakdown, {"netease": 1})

    def test_insert_seeds_empty_breakdown(self) -> None:
        # 回归点：INSERT 时若写 {platform: 1}，紧接着的读回 +1 会重复计数
        import inspect

        import core.database as database

        source = inspect.getsource(database.Statistics.update_today)
        self.assertNotIn('json.dumps({platform: 1})', source)


class ProfanityWarningExpiryTest(unittest.TestCase):
    """过期清理原本只在 push_user_buffer 里做，两项检测都关时永不执行。"""

    def _service(self):
        from app.services.safety.profanity_guard_service import ProfanityGuardService

        runtime = mock.Mock()
        return ProfanityGuardService(runtime)

    def test_expired_warning_is_treated_as_zero(self) -> None:
        service = self._service()
        now = 10_000.0
        service._warnings["u"] = (1, now - service._WARN_EXPIRE_SECONDS - 1)

        self.assertEqual(service._active_warning_count("u", now), 0)
        self.assertNotIn("u", service._warnings, "过期项应顺手清掉")

    def test_fresh_warning_is_kept(self) -> None:
        service = self._service()
        now = 10_000.0
        service._warnings["u"] = (1, now - 1)

        self.assertEqual(service._active_warning_count("u", now), 1)

    def test_unknown_user_is_zero(self) -> None:
        self.assertEqual(self._service()._active_warning_count("nobody", 1.0), 0)

    def test_legacy_int_format_is_not_dropped(self) -> None:
        # 无时间戳时无从判断是否过期，保守当作未过期，别静默丢掉计数
        service = self._service()
        service._warnings["u"] = 1

        self.assertEqual(service._active_warning_count("u", 10_000.0), 1)


class EditUserRoleLockTest(unittest.TestCase):
    """editUserRole 是「读-改-写」且服务端全量覆盖，并发会互相覆盖。"""

    def test_same_user_shares_a_lock(self) -> None:
        from oopz.oopz_api import _user_role_lock

        self.assertIs(_user_role_lock("area-1", "u1"), _user_role_lock("area-1", "u1"))

    def test_different_users_do_not_share_a_lock(self) -> None:
        from oopz.oopz_api import _user_role_lock

        self.assertIsNot(_user_role_lock("area-1", "u1"), _user_role_lock("area-1", "u2"))

    def test_different_areas_do_not_share_a_lock(self) -> None:
        from oopz.oopz_api import _user_role_lock

        self.assertIsNot(_user_role_lock("area-1", "u1"), _user_role_lock("area-2", "u1"))


if __name__ == "__main__":
    unittest.main()
