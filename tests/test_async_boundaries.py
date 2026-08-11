import asyncio
import inspect
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from app.services.interaction.chat_interaction_service import ChatInteractionService  # noqa: E402
from services.conversation_memory import ConversationMemory  # noqa: E402


class _AsyncMemoryStub:
    """严格异步的记忆桩：调用方若漏掉 await，断言会拿到协程对象而失败。"""

    def __init__(self) -> None:
        self.history = [{"role": "user", "content": "上一句"}]
        self.rounds: list[tuple] = []
        self.cleared: list[tuple] = []
        self.cleared_users: list[str] = []

    async def get_history(self, user: str, channel: str) -> list[dict]:
        return self.history

    async def add_round(self, user: str, channel: str, user_msg: str, assistant_msg: str) -> None:
        self.rounds.append((user, channel, user_msg, assistant_msg))

    async def clear(self, user: str, channel: str) -> bool:
        self.cleared.append((user, channel))
        return True

    async def clear_user(self, user: str) -> int:
        self.cleared_users.append(user)
        return 3


def _build_service(memory: object | None) -> ChatInteractionService:
    # sender_of/chat_of 现在是惰性回退，视图自带属性即可，不必再有 infrastructure；
    # 这里仍用 MagicMock 是因为服务还会读取其它运行时字段。
    runtime = mock.MagicMock()
    runtime.sender = mock.AsyncMock()
    runtime.chat = SimpleNamespace(ai_reply=mock.AsyncMock(return_value="回复内容"))
    service = ChatInteractionService(runtime)
    service._memory_init = True
    # 桩对象只满足结构契约，不继承 ConversationMemory
    service._memory = cast("ConversationMemory | None", memory)
    return service


class ConversationMemoryContractTest(unittest.TestCase):
    def test_memory_api_is_asynchronous(self) -> None:
        """记忆层随 Redis 异步化后是异步接口；改回同步时必须同步更新调用方。"""
        for name in ("get_history", "add_round", "clear", "clear_user"):
            with self.subTest(method=name):
                self.assertTrue(
                    inspect.iscoroutinefunction(getattr(ConversationMemory, name)),
                    f"{name} 若改回同步，ChatInteractionService 的调用点需要一并去掉 await",
                )


class ChatInteractionMemoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_mention_fallback_reads_and_writes_memory(self) -> None:
        memory = _AsyncMemoryStub()
        service = _build_service(memory)

        await service.handle_mention_fallback("在吗", "channel-1", "area-1", user="user-1")

        cast("Any", service._chat.ai_reply).assert_awaited_once_with("在吗", history=memory.history)
        self.assertEqual(memory.rounds, [("user-1", "channel-1", "在吗", "回复内容")])
        cast("Any", service._sender.send_message).assert_awaited_once()

    async def test_mention_fallback_skips_memory_without_user(self) -> None:
        memory = _AsyncMemoryStub()
        service = _build_service(memory)

        await service.handle_mention_fallback("在吗", "channel-1", "area-1", user="")

        cast("Any", service._chat.ai_reply).assert_awaited_once_with("在吗", history=None)
        self.assertEqual(memory.rounds, [])

    async def test_mention_fallback_without_memory_still_replies(self) -> None:
        service = _build_service(None)

        await service.handle_mention_fallback("在吗", "channel-1", "area-1", user="user-1")

        cast("Any", service._chat.ai_reply).assert_awaited_once_with("在吗", history=None)
        cast("Any", service._sender.send_message).assert_awaited_once()

    async def test_empty_ai_reply_falls_back_to_hint(self) -> None:
        memory = _AsyncMemoryStub()
        service = _build_service(memory)
        service._chat.ai_reply = mock.AsyncMock(return_value="")

        await service.handle_mention_fallback("在吗", "channel-1", "area-1", user="user-1")

        self.assertEqual(memory.rounds, [])
        args, _ = cast("Any", service._sender.send_message).await_args
        self.assertIn("我没听懂", args[0])

    async def test_clear_memory_returns_sync_result(self) -> None:
        memory = _AsyncMemoryStub()
        service = _build_service(memory)

        self.assertTrue(await service.clear_memory("user-1", "channel-1"))
        self.assertEqual(memory.cleared, [("user-1", "channel-1")])

    async def test_clear_user_memory_returns_sync_count(self) -> None:
        memory = _AsyncMemoryStub()
        service = _build_service(memory)

        self.assertEqual(await service.clear_user_memory("user-1"), 3)
        self.assertEqual(memory.cleared_users, ["user-1"])

    async def test_clear_helpers_are_noops_without_memory(self) -> None:
        service = _build_service(None)

        self.assertFalse(await service.clear_memory("user-1", "channel-1"))
        self.assertEqual(await service.clear_user_memory("user-1"), 0)


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
