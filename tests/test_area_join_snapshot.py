import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from services.area_join_notifier import fetch_member_uid_snapshot


def _members(start: int, count: int) -> list[dict]:
    return [{"uid": f"user-{index}"} for index in range(start, start + count)]


class FakeSender:
    """按 offset 返回预置成员分页的假 sender。"""

    def __init__(self, total_members: int, user_count: int | None = None):
        self._total = total_members
        self._user_count = user_count if user_count is not None else total_members

    def get_area_members(self, area: str, offset_start: int, offset_end: int, quiet: bool = True) -> dict:
        page = [
            {"uid": f"user-{index}"}
            for index in range(offset_start, min(offset_end + 1, self._total))
        ]
        return {"members": page, "userCount": self._user_count}


class FetchMemberSnapshotTest(unittest.TestCase):
    def test_small_area_returns_complete_snapshot(self) -> None:
        uids, rate_limited, truncated = fetch_member_uid_snapshot(FakeSender(150), "area-1")

        self.assertEqual(len(uids), 150)
        self.assertFalse(rate_limited)
        self.assertFalse(truncated)

    def test_stops_early_when_user_count_reached(self) -> None:
        sender = FakeSender(300)
        sender.get_area_members = Mock(side_effect=FakeSender(300).get_area_members)

        uids, _, truncated = fetch_member_uid_snapshot(sender, "area-1", member_fetch_max=5000)

        self.assertEqual(len(uids), 300)
        self.assertFalse(truncated)
        # 300 人 = 3 页，userCount 命中后不应继续翻第 4 页。
        self.assertEqual(sender.get_area_members.call_count, 3)

    def test_over_cap_area_is_marked_truncated(self) -> None:
        uids, rate_limited, truncated = fetch_member_uid_snapshot(
            FakeSender(1200), "area-1", member_fetch_max=1000
        )

        self.assertTrue(truncated, "超过翻页上限必须标记快照不完整")
        self.assertEqual(len(uids), 1000)
        self.assertFalse(rate_limited)

    def test_rate_limit_error_is_reported(self) -> None:
        sender = Mock()
        sender.get_area_members = Mock(return_value={"error": "HTTP 429 too many requests"})

        uids, rate_limited, truncated = fetch_member_uid_snapshot(sender, "area-1")

        self.assertIsNone(uids)
        self.assertTrue(rate_limited)
        self.assertFalse(truncated)

    def test_generic_error_is_not_rate_limit(self) -> None:
        sender = Mock()
        sender.get_area_members = Mock(return_value={"error": "HTTP 500 boom"})

        uids, rate_limited, truncated = fetch_member_uid_snapshot(sender, "area-1")

        self.assertIsNone(uids)
        self.assertFalse(rate_limited)
        self.assertFalse(truncated)


if __name__ == "__main__":
    unittest.main()
