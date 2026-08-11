"""多大区并行搜索的确定性与提前返回。
"""

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.lol_fa8.service import FA8Handler  # noqa: E402


class _FakeClient:
    """按大区给出预置的延迟与结果，并记录哪些查询被真正跑完。"""

    def __init__(self, plan: dict[str, tuple[float, Any]]):
        self._plan = plan
        self.started: list[str] = []
        self.completed: list[str] = []
        self.cancelled: list[str] = []

    async def query_summoner(self, name: str, area: str):
        self.started.append(area)
        delay, outcome = self._plan[area]
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled.append(area)
            raise
        self.completed.append(area)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _handler(plan: dict[str, tuple[float, Any]]) -> tuple[FA8Handler, _FakeClient]:
    handler = FA8Handler.__new__(FA8Handler)
    client = _FakeClient(plan)
    cast("Any", handler)._client = client
    return handler, client


_HIT = {"code": 0, "name": "命中"}
_MISS = {"code": 1}


class AreaSearchDeterminismTest(unittest.IsolatedAsyncioTestCase):
    async def test_configured_order_wins_over_response_order(self) -> None:
        """靠后的大区先响应，也不能抢走靠前大区的结果。"""
        handler, client = _handler({
            "a": (0.05, _HIT),   # 慢，但配置在前
            "b": (0.00, _HIT),   # 快，但配置在后
        })

        result = await handler._search_summoner("某人", ["a", "b"])

        assert result is not None
        self.assertEqual(result[0], "a")
        # 两个都发起了，说明确实是并发而非串行试探
        self.assertEqual(sorted(client.started), ["a", "b"])

    async def test_result_is_stable_across_runs(self) -> None:
        """同样的输入重复多次必须给同一个大区，不能随机。"""
        seen = set()
        for _ in range(5):
            handler, _client = _handler({"a": (0.02, _HIT), "b": (0.00, _HIT)})
            result = await handler._search_summoner("某人", ["a", "b"])
            assert result is not None
            seen.add(result[0])
        self.assertEqual(seen, {"a"})


class AreaSearchEarlyReturnTest(unittest.IsolatedAsyncioTestCase):
    async def test_hit_does_not_wait_for_slower_areas(self) -> None:
        """首个大区命中后，不该再等剩下那些会拖到超时的区。"""
        handler, client = _handler({
            "a": (0.00, _HIT),
            "b": (5.00, _HIT),   # 模拟卡到超时的大区
            "c": (5.00, _HIT),
        })

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        result = await handler._search_summoner("某人", ["a", "b", "c"])
        elapsed = loop.time() - started_at

        assert result is not None
        self.assertEqual(result[0], "a")
        self.assertLess(elapsed, 1.0, "命中后不得再等慢区")
        self.assertEqual(client.completed, ["a"], "慢区不该跑完")
        self.assertEqual(sorted(client.cancelled), ["b", "c"], "剩余请求必须被取消")

    async def test_leaves_no_pending_tasks_behind(self) -> None:
        """取消之后要等它们真正结束，否则会留下悬挂任务。"""
        handler, _client = _handler({
            "a": (0.00, _HIT),
            "b": (5.00, _HIT),
        })
        before = len(asyncio.all_tasks())

        await handler._search_summoner("某人", ["a", "b"])
        await asyncio.sleep(0)

        self.assertLessEqual(len(asyncio.all_tasks()), before)

    async def test_earlier_failures_do_not_block_later_hit(self) -> None:
        handler, client = _handler({
            "a": (0.00, _MISS),
            "b": (0.00, _HIT),
        })

        result = await handler._search_summoner("某人", ["a", "b"])

        assert result is not None
        self.assertEqual(result[0], "b")
        self.assertEqual(client.completed, ["a", "b"])

    async def test_exception_in_one_area_is_skipped(self) -> None:
        """某个大区抛错不能让整次搜索失败。"""
        handler, _client = _handler({
            "a": (0.00, RuntimeError("网络错误")),
            "b": (0.00, _HIT),
        })

        result = await handler._search_summoner("某人", ["a", "b"])

        assert result is not None
        self.assertEqual(result[0], "b")

    async def test_all_areas_miss_returns_none(self) -> None:
        handler, _client = _handler({"a": (0.00, _MISS), "b": (0.00, _MISS)})

        self.assertIsNone(await handler._search_summoner("某人", ["a", "b"]))

    async def test_single_area_skips_task_machinery(self) -> None:
        handler, client = _handler({"a": (0.00, _HIT)})

        result = await handler._search_summoner("某人", ["a"])

        assert result is not None
        self.assertEqual(result[0], "a")
        self.assertEqual(client.started, ["a"])


if __name__ == "__main__":
    unittest.main()
