import asyncio
import inspect
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))



class TaskSupervisorContractTest(unittest.IsolatedAsyncioTestCase):
    """关停期拒绝新任务时必须关闭协程，否则会留下 never awaited 告警。"""

    async def test_create_after_close_closes_the_coroutine(self) -> None:
        from app.lifecycle.task_supervisor import TaskSupervisor

        supervisor = TaskSupervisor()
        await supervisor.close(timeout=0.1)

        async def never_runs() -> None:  # pragma: no cover - 不应被执行
            raise AssertionError("关停后不应执行新任务")

        coroutine = never_runs()
        with self.assertRaises(RuntimeError):
            supervisor.create(coroutine, name="rejected")

        self.assertEqual(inspect.getcoroutinestate(coroutine), inspect.CORO_CLOSED)

    async def test_created_task_is_tracked_and_cancelled_on_close(self) -> None:
        from app.lifecycle.task_supervisor import TaskSupervisor

        supervisor = TaskSupervisor()
        started = asyncio.Event()

        async def long_running() -> None:
            started.set()
            await asyncio.sleep(3600)

        task = supervisor.create(long_running(), name="long")
        await started.wait()
        self.assertIn(task, supervisor.tasks)

        self.assertTrue(await supervisor.close(timeout=1.0))
        self.assertTrue(task.cancelled())


if __name__ == "__main__":
    unittest.main()
