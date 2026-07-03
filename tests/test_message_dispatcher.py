import sys
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from core.message_dispatcher import MessageDispatcher


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

    def test_stop_terminates_workers(self) -> None:
        dispatcher = MessageDispatcher(workers=2, maxsize=8)
        dispatcher.submit("k", lambda: None)
        threads = list(dispatcher._threads)
        dispatcher.stop(timeout=2)

        self.assertTrue(_wait_until(lambda: all(not t.is_alive() for t in threads)))


if __name__ == "__main__":
    unittest.main()
