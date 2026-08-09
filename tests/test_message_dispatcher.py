import queue
import sys
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from core.message_dispatcher import MessageDispatcher  # noqa: E402


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class MessageDispatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dispatcher = MessageDispatcher(workers=2, maxsize=64)

    def tearDown(self) -> None:
        self.dispatcher.stop(timeout=2)

    def test_same_key_preserves_order(self) -> None:
        dispatcher = MessageDispatcher(workers=2, maxsize=500)
        try:
            seen: list[int] = []
            for index in range(200):
                dispatcher.submit("area-1:channel-1", seen.append, index)

            self.assertTrue(_wait_until(lambda: len(seen) == 200))
            self.assertEqual(seen, list(range(200)))
        finally:
            dispatcher.stop(timeout=2)

    def test_slow_key_does_not_block_other_key(self) -> None:
        # 找两个落在不同分片上的 key，避免哈希碰撞使测试失效。
        worker_count = 2
        key_a = "key-a"
        key_b = next(
            f"key-{i}"
            for i in range(1000)
            if hash(f"key-{i}") % worker_count != hash(key_a) % worker_count
        )

        release = threading.Event()
        fast_done = threading.Event()

        self.dispatcher.submit(key_a, release.wait, 5)
        self.dispatcher.submit(key_b, fast_done.set)

        self.assertTrue(
            fast_done.wait(timeout=2),
            "另一分片的任务不应被慢任务阻塞",
        )
        release.set()

    def test_full_queue_drops_and_reports(self) -> None:
        dispatcher = MessageDispatcher(workers=1, maxsize=1)
        try:
            blocker = threading.Event()
            # 第一条占住工作线程，第二条填满队列，第三条必然被丢弃。
            dispatcher.submit("k", blocker.wait, 5)
            time.sleep(0.05)
            dispatcher.submit("k", lambda: None)
            dropped = dispatcher.submit("k", lambda: None)

            self.assertFalse(dropped)
            self.assertGreaterEqual(dispatcher.dropped_count, 1)
            blocker.set()
        finally:
            dispatcher.stop(timeout=2)

    def test_task_exception_does_not_kill_worker(self) -> None:
        done = threading.Event()

        def _boom() -> None:
            raise RuntimeError("boom")

        self.dispatcher.submit("k", _boom)
        self.dispatcher.submit("k", done.set)

        self.assertTrue(done.wait(timeout=2), "异常任务之后工作线程应继续消费")

    def test_stop_delivers_sentinel_even_when_queue_was_full(self) -> None:
        """队列满时 sentinel 原先被 put_nowait 静默丢弃，工作线程永远收不到停止信号。

        表现是 stop() 白等满 timeout 后返回，线程仍活着阻塞在 get() 上，
        积压随进程退出一起丢掉。
        """
        dispatcher = MessageDispatcher(workers=1, maxsize=2)
        done: list[int] = []
        gate = threading.Event()
        try:
            dispatcher.submit("k", gate.wait, 5)  # 占住工作线程
            time.sleep(0.05)
            self.assertTrue(dispatcher.submit("k", done.append, 0))
            self.assertTrue(dispatcher.submit("k", done.append, 1))
            self.assertFalse(dispatcher.submit("k", done.append, 2), "此时队列应已满")

            thread = dispatcher._threads[0]
            gate.set()
            self.assertTrue(dispatcher.stop(timeout=3))

            self.assertFalse(thread.is_alive(), "工作线程应收到停止信号并退出")
            self.assertEqual(done, [0, 1], "积压必须处理完再退出")
        finally:
            gate.set()

    def test_submit_is_rejected_while_stopping(self) -> None:
        # 关停期间还收新消息的话，队列永远排不干净，drain 没有终止性
        dispatcher = MessageDispatcher(workers=1, maxsize=8)
        dispatcher.submit("k", lambda: None)
        self.assertTrue(dispatcher.stop(timeout=2))

        self.assertFalse(dispatcher.submit("k", lambda: None))

    def test_stop_before_start_is_permanent_and_rejects_submit(self) -> None:
        dispatcher = MessageDispatcher(workers=1, maxsize=8)

        self.assertTrue(dispatcher.stop(timeout=0))
        dispatcher.start()

        self.assertFalse(dispatcher.submit("k", lambda: None))
        self.assertEqual(dispatcher._threads, [])

    def test_timeout_cancels_waiting_tasks_and_worker_eventually_exits(self) -> None:
        dispatcher = MessageDispatcher(workers=1, maxsize=8)
        entered = threading.Event()
        release = threading.Event()
        queued_results: list[int] = []

        def blocking_task() -> None:
            entered.set()
            release.wait(timeout=2)

        try:
            self.assertTrue(dispatcher.submit("k", blocking_task))
            self.assertTrue(entered.wait(timeout=1))
            self.assertTrue(dispatcher.submit("k", queued_results.append, 1))
            self.assertTrue(dispatcher.submit("k", queued_results.append, 2))
            worker = dispatcher._threads[0]

            started_at = time.monotonic()
            fully_stopped = dispatcher.stop(timeout=0.03)
            elapsed = time.monotonic() - started_at

            self.assertFalse(fully_stopped, "in-flight 任务未退出时必须返回 False")
            self.assertLess(elapsed, 0.2, "stop 不得超出总预算长时阻塞")
            self.assertEqual(queued_results, [], "超时时应取消尚未开始的任务")
            self.assertFalse(dispatcher.submit("k", queued_results.append, 3))
            self.assertTrue(worker.is_alive())

            release.set()
            self.assertTrue(
                _wait_until(lambda: not worker.is_alive()),
                "in-flight 任务返回后 worker 应收到 sentinel 并退出",
            )
            self.assertEqual(queued_results, [])
            self.assertTrue(dispatcher.stop(timeout=0.1))
        finally:
            release.set()
            dispatcher.stop(timeout=1)

    def test_submit_cannot_land_behind_stop_sentinel(self) -> None:
        """submit 一旦返回 True，任务就必须排在关停 sentinel 之前。"""

        class _PausingQueue(queue.Queue):
            def __init__(self):
                super().__init__(maxsize=8)
                self.at_put = threading.Event()
                self.release_put = threading.Event()

            def put_nowait(self, item):
                self.at_put.set()
                self.release_put.wait(timeout=2)
                return super().put_nowait(item)

        dispatcher = MessageDispatcher(workers=1, maxsize=8)
        shard = _PausingQueue()
        dispatcher._queues = [shard]
        task_ran = threading.Event()
        submit_result = []
        stop_done = threading.Event()

        submitter = threading.Thread(
            target=lambda: submit_result.append(dispatcher.submit("k", task_ran.set))
        )
        submitter.start()
        self.assertTrue(shard.at_put.wait(timeout=1))

        def _stop() -> None:
            dispatcher.stop(timeout=2)
            stop_done.set()

        stopper = threading.Thread(target=_stop)
        stopper.start()
        self.assertFalse(
            stop_done.wait(timeout=0.05),
            "关停应等待已通过状态检查的 submit 完成入队",
        )

        shard.release_put.set()
        submitter.join(timeout=1)
        stopper.join(timeout=2)

        self.assertEqual(submit_result, [True])
        self.assertTrue(task_ran.is_set())
        self.assertTrue(stop_done.is_set())
        self.assertEqual(shard.qsize(), 0)

    def test_stop_is_idempotent(self) -> None:
        dispatcher = MessageDispatcher(workers=2, maxsize=8)
        dispatcher.submit("k", lambda: None)
        self.assertTrue(dispatcher.stop(timeout=2))
        self.assertTrue(dispatcher.stop(timeout=2))  # 不应抛异常

    def test_stop_terminates_workers(self) -> None:
        dispatcher = MessageDispatcher(workers=2, maxsize=8)
        dispatcher.submit("k", lambda: None)
        threads = list(dispatcher._threads)
        self.assertTrue(dispatcher.stop(timeout=2))

        self.assertTrue(_wait_until(lambda: all(not t.is_alive() for t in threads)))


if __name__ == "__main__":
    unittest.main()
