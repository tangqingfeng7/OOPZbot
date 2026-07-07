import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from services.area_join_notifier import (
    OPERATE_LOG_MEMBER_OP_TYPES,
    AreaOperateLogCursor,
    fetch_member_uid_snapshot,
    fetch_operate_log_changes,
    is_operate_log_permission_denied,
    parse_area_operate_log_changes,
)


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


class AreaOperateLogChangeTest(unittest.TestCase):
    def test_parse_join_and_leave_logs(self) -> None:
        changes = parse_area_operate_log_changes(
            "area-1",
            {
                "logs": [
                    {"optUid": "user-1", "content": "加入域", "createTime": 100},
                    {"optUid": "user-1", "content": "退出域", "createTime": 110},
                    {"optUid": "user-2", "content": "移出域", "createTime": 120},
                ]
            },
        )

        self.assertEqual([c.action for c in changes], ["join", "leave"])
        self.assertEqual([c.uid for c in changes], ["user-1", "user-1"])
        self.assertEqual([c.create_time for c in changes], [100, 110])

    def test_cursor_skips_initial_logs_and_consumes_new_logs_once(self) -> None:
        cursor = AreaOperateLogCursor()
        first_batch = parse_area_operate_log_changes(
            "area-1",
            {"logs": [{"optUid": "user-1", "content": "加入域", "createTime": 100}]},
        )

        self.assertEqual(cursor.consume("area-1", first_batch), [])
        self.assertEqual(cursor.consume("area-1", first_batch), [])

        second_batch = parse_area_operate_log_changes(
            "area-1",
            {
                "logs": [
                    {"optUid": "user-1", "content": "加入域", "createTime": 100},
                    {"optUid": "user-2", "content": "退出域", "createTime": 110},
                ]
            },
        )

        fresh = cursor.consume("area-1", second_batch)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0].uid, "user-2")
        self.assertEqual(fresh[0].action, "leave")

    def test_fetch_operate_log_changes_uses_member_op_filters(self) -> None:
        sender = Mock()
        sender.get_area_operate_logs = Mock(
            return_value={"logs": [{"optUid": "user-1", "content": "加入域", "createTime": 100}]}
        )

        changes, rate_limited, error = fetch_operate_log_changes(sender, "area-1")

        self.assertFalse(rate_limited)
        self.assertEqual(error, "")
        self.assertEqual(len(changes), 1)
        sender.get_area_operate_logs.assert_called_once_with(
            area="area-1",
            offset=0,
            op_types=OPERATE_LOG_MEMBER_OP_TYPES,
        )

    def test_fetch_operate_log_changes_reports_permission_denied_error(self) -> None:
        sender = Mock()
        sender.get_area_operate_logs = Mock(return_value={"error": "暂无进行此操作的权限"})

        changes, rate_limited, error = fetch_operate_log_changes(sender, "area-1")

        self.assertIsNone(changes)
        self.assertFalse(rate_limited)
        self.assertTrue(is_operate_log_permission_denied(error))


if __name__ == "__main__":
    unittest.main()
