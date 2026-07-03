import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import core.queue_manager as qm


class RedisFallbackRecoveryTest(unittest.TestCase):
    """内存降级后应周期性探测真实 Redis 并自动切回。"""

    def setUp(self) -> None:
        self._saved_client = qm._redis_client
        self._saved_retry = qm._last_redis_retry

    def tearDown(self) -> None:
        qm._redis_client = self._saved_client
        qm._last_redis_retry = self._saved_retry

    def test_falls_back_to_memory_when_redis_down(self) -> None:
        with patch.object(qm.redis, "Redis", side_effect=ConnectionError("down")):
            client = qm.get_redis_client(force_reset=True)

        self.assertIsInstance(client, qm._InMemoryRedis)

    def test_recovers_to_real_redis_after_retry_interval(self) -> None:
        with patch.object(qm.redis, "Redis", side_effect=ConnectionError("down")):
            fallback = qm.get_redis_client(force_reset=True)
        self.assertIsInstance(fallback, qm._InMemoryRedis)

        real_client = Mock(name="RealRedis")
        real_client.ping = Mock(return_value=True)
        # 冷却期内不应重试
        with patch.object(qm.redis, "Redis", return_value=real_client):
            still_memory = qm.get_redis_client()
            self.assertIs(still_memory, fallback)

            # 冷却期过后自动切回真实 Redis
            qm._last_redis_retry = 0.0
            recovered = qm.get_redis_client()

        self.assertIs(recovered, real_client)

    def test_queue_manager_instances_follow_recovery(self) -> None:
        with patch.object(qm.redis, "Redis", side_effect=ConnectionError("down")):
            qm.get_redis_client(force_reset=True)
            queue = qm.QueueManager(area="test-area")
            self.assertIsInstance(queue.redis, qm._InMemoryRedis)

        real_client = Mock(name="RealRedis")
        real_client.ping = Mock(return_value=True)
        with patch.object(qm.redis, "Redis", return_value=real_client):
            qm._last_redis_retry = 0.0
            self.assertIs(queue.redis, real_client, "已存在的 QueueManager 应自动切回真实 Redis")

    def test_retry_failure_keeps_memory_fallback(self) -> None:
        with patch.object(qm.redis, "Redis", side_effect=ConnectionError("down")):
            fallback = qm.get_redis_client(force_reset=True)
            qm._last_redis_retry = 0.0
            still_memory = qm.get_redis_client()

        self.assertIs(still_memory, fallback)


class ConversationMemoryProviderTest(unittest.TestCase):
    """ConversationMemory 应通过 provider 现取客户端，跟随 Redis 恢复。"""

    def test_memory_follows_provider_swap(self) -> None:
        from services.conversation_memory import ConversationMemory

        first = Mock(name="MemoryClient")
        first.get = Mock(return_value=None)
        second = Mock(name="RealClient")
        second.get = Mock(return_value=None)
        current = {"client": first}

        memory = ConversationMemory(lambda: current["client"], max_rounds=5)

        memory.get_history("user-1", "channel-1")
        first.get.assert_called_once()

        current["client"] = second
        memory.get_history("user-1", "channel-1")
        second.get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
