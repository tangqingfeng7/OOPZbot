"""消息分发器的分片、背压与关停语义。

分发器已从线程池改为 asyncio 任务模型：分片是 `asyncio.Queue`，worker 是
`asyncio.Task`（`_workers`，不再有 `_threads`），`stop()` 为协程。以下用例
按新模型重写，守住的行为与线程版一致。
"""

import asyncio
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from core.message_dispatcher import MessageDispatcher  # noqa: E402


async def _wait_until(predicate, timeout: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


def _other_shard_key(worker_count: int, key: str) -> str:
    """找一个落在不同分片上的 key，避免哈希碰撞让并发用例失去意义。"""
    return next(
        f"key-{i}"
        for i in range(10000)
        if hash(f"key-{i}") % worker_count != hash(key) % worker_count
    )


class MessageDispatcherTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.dispatcher = MessageDispatcher(workers=2, maxsize=64)

    async def asyncTearDown(self) -> None:
        await self.dispatcher.stop(timeout=2)

    async def test_same_key_preserves_order(self) -> None:
        dispatcher = MessageDispatcher(workers=2, maxsize=500)
        try:
            seen: list[int] = []
            for index in range(200):
                dispatcher.submit("area-1:channel-1", seen.append, index)

            self.assertTrue(await _wait_until(lambda: len(seen) == 200))
            self.assertEqual(seen, list(range(200)))
        finally:
            await dispatcher.stop(timeout=2)

    async def test_slow_key_does_not_block_other_key(self) -> None:
        key_a = "key-a"
        key_b = _other_shard_key(2, key_a)

        release = asyncio.Event()
        fast_done = asyncio.Event()

        async def slow() -> None:
            await asyncio.wait_for(release.wait(), timeout=5)

        self.dispatcher.submit(key_a, slow)
        self.dispatcher.submit(key_b, fast_done.set)

        try:
            await asyncio.wait_for(fast_done.wait(), timeout=2)
        except asyncio.TimeoutError:
            self.fail("另一分片的任务不应被慢任务阻塞")
        finally:
            release.set()

    async def test_full_queue_drops_and_reports(self) -> None:
        dispatcher = MessageDispatcher(workers=1, maxsize=1)
        release = asyncio.Event()
        started = asyncio.Event()

        async def blocker() -> None:
            started.set()
            await asyncio.wait_for(release.wait(), timeout=5)

        try:
            # 第一条被 worker 取走后队列才空出来，否则第二条会直接判满
            dispatcher.submit("k", blocker)
            await asyncio.wait_for(started.wait(), timeout=1)

            self.assertTrue(dispatcher.submit("k", lambda: None))
            dropped = dispatcher.submit("k", lambda: None)

            self.assertFalse(dropped)
            self.assertGreaterEqual(dispatcher.dropped_count, 1)
        finally:
            release.set()
            await dispatcher.stop(timeout=2)

    async def test_task_exception_does_not_kill_worker(self) -> None:
        done = asyncio.Event()

        def _boom() -> None:
            raise RuntimeError("boom")

        self.dispatcher.submit("k", _boom)
        self.dispatcher.submit("k", done.set)

        try:
            await asyncio.wait_for(done.wait(), timeout=2)
        except asyncio.TimeoutError:
            self.fail("异常任务之后 worker 应继续消费同分片的后续任务")

    async def test_async_task_exception_does_not_kill_worker(self) -> None:
        """worker 会 await 可等待的返回值，协程任务抛错同样不能带崩 worker。"""
        done = asyncio.Event()

        async def _boom() -> None:
            raise RuntimeError("boom")

        self.dispatcher.submit("k", _boom)
        self.dispatcher.submit("k", done.set)

        try:
            await asyncio.wait_for(done.wait(), timeout=2)
        except asyncio.TimeoutError:
            self.fail("协程任务抛错后 worker 应继续消费")

    async def test_stop_drains_backlog_before_workers_exit(self) -> None:
        """关停必须先排空积压再让 worker 退出，否则积压随进程一起丢掉。"""
        dispatcher = MessageDispatcher(workers=1, maxsize=2)
        done: list[int] = []
        gate = asyncio.Event()
        started = asyncio.Event()

        async def blocker() -> None:
            started.set()
            await asyncio.wait_for(gate.wait(), timeout=5)

        try:
            dispatcher.submit("k", blocker)
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertTrue(dispatcher.submit("k", done.append, 0))
            self.assertTrue(dispatcher.submit("k", done.append, 1))
            self.assertFalse(dispatcher.submit("k", done.append, 2), "此时队列应已满")

            gate.set()
            self.assertTrue(await dispatcher.stop(timeout=3))

            self.assertEqual(done, [0, 1], "积压必须处理完再退出")
            self.assertTrue(all(task.done() for task in dispatcher._workers))
        finally:
            gate.set()

    async def test_submit_is_rejected_while_stopping(self) -> None:
        # 关停期间还收新消息的话，队列永远排不干净，drain 没有终止性
        dispatcher = MessageDispatcher(workers=1, maxsize=8)
        dispatcher.submit("k", lambda: None)
        self.assertTrue(await dispatcher.stop(timeout=2))

        self.assertFalse(dispatcher.submit("k", lambda: None))

    async def test_stop_before_start_is_permanent_and_rejects_submit(self) -> None:
        dispatcher = MessageDispatcher(workers=1, maxsize=8)

        self.assertTrue(await dispatcher.stop(timeout=0))
        dispatcher.start()

        self.assertFalse(dispatcher.submit("k", lambda: None))
        self.assertEqual(dispatcher._workers, [])

    async def test_timeout_cancels_waiting_tasks_and_does_not_leak_workers(self) -> None:
        """in-flight 任务超出预算时：未开始的任务被取消，stop 返回 False，且不留活 worker。"""
        dispatcher = MessageDispatcher(workers=1, maxsize=8)
        entered = asyncio.Event()
        release = asyncio.Event()
        queued_results: list[int] = []

        async def blocking_task() -> None:
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=2)

        try:
            self.assertTrue(dispatcher.submit("k", blocking_task))
            await asyncio.wait_for(entered.wait(), timeout=1)
            self.assertTrue(dispatcher.submit("k", queued_results.append, 1))
            self.assertTrue(dispatcher.submit("k", queued_results.append, 2))
            workers = list(dispatcher._workers)

            loop = asyncio.get_running_loop()
            started_at = loop.time()
            fully_stopped = await dispatcher.stop(timeout=0.03)
            elapsed = loop.time() - started_at

            self.assertFalse(fully_stopped, "in-flight 任务未退出时必须返回 False")
            self.assertLess(elapsed, 1.0, "stop 不得超出总预算长时阻塞")
            self.assertEqual(queued_results, [], "超时时应取消尚未开始的任务")
            self.assertFalse(dispatcher.submit("k", queued_results.append, 3))
            # 超预算的 worker 会被取消，不能留下悬挂任务
            self.assertTrue(all(task.done() for task in workers))
        finally:
            release.set()
            await dispatcher.stop(timeout=1)

    async def test_accepted_submit_always_runs_before_stop_completes(self) -> None:
        """submit 一旦返回 True，任务就必须在 stop 返回前跑完。

        线程版这里存在 submit 与 stop 交错的竞态；改成 asyncio 后 submit 在
        状态检查与入队之间没有 await 点，竞态由结构消除，这里守住对外可见的保证。
        """
        dispatcher = MessageDispatcher(workers=1, maxsize=64)
        ran: list[int] = []

        accepted = [dispatcher.submit("k", ran.append, i) for i in range(20)]
        self.assertTrue(all(accepted))

        self.assertTrue(await dispatcher.stop(timeout=3))
        self.assertEqual(ran, list(range(20)))

    async def test_stop_is_idempotent(self) -> None:
        dispatcher = MessageDispatcher(workers=2, maxsize=8)
        dispatcher.submit("k", lambda: None)
        self.assertTrue(await dispatcher.stop(timeout=2))
        self.assertTrue(await dispatcher.stop(timeout=2))  # 不应抛异常

    async def test_stop_terminates_workers(self) -> None:
        dispatcher = MessageDispatcher(workers=2, maxsize=8)
        dispatcher.submit("k", lambda: None)
        workers = list(dispatcher._workers)
        self.assertTrue(await dispatcher.stop(timeout=2))

        self.assertTrue(await _wait_until(lambda: all(task.done() for task in workers)))
        self.assertEqual(dispatcher._workers, [])


if __name__ == "__main__":
    unittest.main()
