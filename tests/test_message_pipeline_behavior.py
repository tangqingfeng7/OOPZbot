import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


from app.services.runtime import CommandRuntimeView  # noqa: E402


def _runtime(**components: object) -> CommandRuntimeView:
    """把用例所需的最小运行时切片标记为服务协议测试桩。"""

    return cast(CommandRuntimeView, SimpleNamespace(**components))


def _build_recent_store():
    from app.services.runtime import RecentMessageStore

    return RecentMessageStore()


class MessageContextTest(unittest.TestCase):
    def test_message_context_normalizes_message_payload(self) -> None:
        from app.services.routing.command_message_service import MessageContext

        ctx = MessageContext.from_message(
            {
                "content": "  /help  ",
                "channel": "channel-1",
                "area": "area-1",
                "person": "user-1",
                "messageId": "msg-1",
                "timestamp": "ts-1",
            }
        )

        self.assertEqual(ctx.content, "/help")
        self.assertEqual(ctx.channel, "channel-1")
        self.assertEqual(ctx.area, "area-1")
        self.assertEqual(ctx.user, "user-1")
        self.assertEqual(ctx.message_id, "msg-1")
        self.assertEqual(ctx.timestamp, "ts-1")
        self.assertTrue(ctx.is_slash_command())
        self.assertFalse(ctx.is_mention_command("(met)bot(met)"))

    def test_message_context_extracts_mention_text(self) -> None:
        from app.services.routing.command_message_service import MessageContext

        ctx = MessageContext.from_message(
            {
                "content": "(met)bot(met) help",
                "channel": "channel-1",
                "area": "area-1",
                "person": "user-1",
                "messageId": "msg-1",
            }
        )

        self.assertTrue(ctx.is_mention_command("(met)bot(met)"))
        self.assertTrue(ctx.is_command("(met)bot(met)"))
        self.assertEqual(ctx.mention_text("(met)bot(met)"), "help")


class CommandMessageServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sender = AsyncMock()
        self.chat = Mock()
        self.chat.ai_reply = AsyncMock()
        self.chat.check_profanity = AsyncMock()
        self.chat.close = AsyncMock()
        self.chat.generate_image = AsyncMock()
        self.access = Mock()
        self.profanity = Mock()
        # 只有 handle_profanity 是协程，检测与缓冲区都是同步的
        self.profanity.handle_profanity = AsyncMock()
        self.command = Mock()
        self.runtime = _runtime(
            sender=self.sender,
            chat=self.chat,
            bot_uid="bot-uid",
            bot_mention="(met)bot(met)",
            services=SimpleNamespace(
                routing=SimpleNamespace(access=self.access, command=self.command),
                safety=SimpleNamespace(profanity=self.profanity),
            ),
            recent_messages=_build_recent_store(),
        )

    def _build_service(self):
        from app.services.routing.command_message_service import CommandMessageService

        return CommandMessageService(self.runtime)

    def test_remember_message_appends_and_limits_recent_messages(self) -> None:
        from app.services.routing.command_message_service import MessageContext

        service = self._build_service()
        for index in range(55):
            ctx = MessageContext(
                raw={},
                content=f"message-{index}",
                channel="channel",
                area="area",
                user="user",
                message_id=f"id-{index}",
                timestamp=f"ts-{index}",
            )
            service.remember_message(ctx)

        recent_messages = list(self.runtime.recent_messages)
        self.assertEqual(len(recent_messages), 50)
        self.assertEqual(recent_messages[0]["messageId"], "id-5")
        self.assertEqual(recent_messages[-1]["messageId"], "id-54")

    async def test_handle_profanity_short_circuits_on_direct_keyword_match(self) -> None:
        import app.services.routing.command_message_service as module
        from app.services.routing.command_message_service import MessageContext

        service = self._build_service()
        ctx = MessageContext(
            raw={},
            content="bad",
            channel="channel",
            area="area",
            user="user-1",
            message_id="msg-1",
            timestamp="ts-1",
        )
        self.profanity.check_profanity.return_value = "bad"

        with patch.object(module, "PROFANITY_CONFIG", {"enabled": True, "skip_admins": False}):
            result = await service.handle_profanity(ctx)

        self.assertTrue(result)
        self.profanity.handle_profanity.assert_called_once_with(
            "user-1",
            "channel",
            "area",
            "bad",
            [{"message_id": "msg-1", "channel": "channel", "area": "area", "timestamp": "ts-1"}],
        )

    async def test_handle_profanity_can_use_ai_context_detection(self) -> None:
        import app.services.routing.command_message_service as module
        from app.services.routing.command_message_service import MessageContext

        service = self._build_service()
        ctx = MessageContext(
            raw={},
            content="part-1",
            channel="channel",
            area="area",
            user="user-1",
            message_id="msg-1",
            timestamp="ts-1",
        )
        self.profanity.check_profanity.return_value = None
        self.profanity.check_context_profanity.return_value = None
        self.profanity.clean_text.return_value = "part-1"
        self.profanity.get_user_buffer.return_value = [
            {"content": "part-1"},
            {"content": "part-2"},
        ]
        self.chat.check_profanity.side_effect = [None, "joined violation"]

        config = {
            "enabled": True,
            "skip_admins": False,
            "context_detection": True,
            "ai_detection": True,
            "ai_min_length": 2,
        }
        with patch.object(module, "PROFANITY_CONFIG", config):
            result = await service.handle_profanity(ctx)

        self.assertTrue(result)
        self.profanity.push_user_buffer.assert_called_once_with(
            "user-1",
            "part-1",
            "msg-1",
            "channel",
            "area",
            "ts-1",
        )
        self.profanity.handle_profanity.assert_called_once_with(
            "user-1",
            "channel",
            "area",
            "AI:joined violation",
            list(self.profanity.get_user_buffer.return_value),
        )

    async def test_reject_unauthorized_command_sends_denial_message(self) -> None:
        from app.services.routing.command_message_service import MessageContext

        service = self._build_service()
        ctx = MessageContext(
            raw={},
            content="/ban user",
            channel="channel",
            area="area",
            user="user-1",
            message_id="msg-1",
            timestamp="ts-1",
        )
        self.access.is_admin.return_value = False
        self.access.is_public_command.return_value = False

        result = await service.reject_unauthorized_command(ctx)

        self.assertTrue(result)
        self.sender.send_message.assert_called_once()
        self.assertIn("[x]", self.sender.send_message.call_args.args[0])


class CommandRouterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.mention = Mock()
        self.mention.dispatch = AsyncMock(return_value=False)
        self.slash = Mock()
        self.slash.dispatch = AsyncMock(return_value=True)
        self.chat = Mock()
        self.chat.handle_plain_chat = AsyncMock(return_value=True)
        self.chat.ai_reply = AsyncMock()
        self.chat.check_profanity = AsyncMock()
        self.chat.close = AsyncMock()
        self.chat.generate_image = AsyncMock()
        self.recall_scheduler = Mock()
        self.recall_scheduler.cancel_all = AsyncMock()
        self.recall_scheduler.schedule_recall = AsyncMock()
        self.recall_scheduler.schedule_user_message_recall = AsyncMock()
        self.recall_scheduler.stop = AsyncMock()
        self.runtime = _runtime(
            bot_mention="(met)bot(met)",
            services=SimpleNamespace(
                routing=SimpleNamespace(mention=self.mention, slash=self.slash),
                interaction=SimpleNamespace(chat=self.chat),
                safety=SimpleNamespace(recall_scheduler=self.recall_scheduler),
            ),
        )

    def _build_router(self):
        from app.services.routing.command_router import CommandRouter

        return CommandRouter(self.runtime)

    async def test_route_mention_dispatches_and_schedules_recall(self) -> None:
        from app.services.routing.command_message_service import MessageContext

        router = self._build_router()
        ctx = MessageContext(
            raw={},
            content="(met)bot(met) help",
            channel="channel",
            area="area",
            user="user-1",
            message_id="msg-1",
            timestamp="ts-1",
        )

        await router.route(ctx)

        self.mention.dispatch.assert_called_once_with("help", "channel", "area", "user-1")
        self.recall_scheduler.schedule_user_message_recall.assert_called_once_with(
            "msg-1",
            "channel",
            "area",
            "ts-1",
        )

    async def test_route_slash_dispatches_and_schedules_recall(self) -> None:
        from app.services.routing.command_message_service import MessageContext

        router = self._build_router()
        ctx = MessageContext(
            raw={},
            content="/help",
            channel="channel",
            area="area",
            user="user-1",
            message_id="msg-1",
            timestamp="ts-1",
        )

        await router.route(ctx)

        self.slash.dispatch.assert_called_once_with("/help", "channel", "area", "user-1")
        self.recall_scheduler.schedule_user_message_recall.assert_called_once_with(
            "msg-1",
            "channel",
            "area",
            "ts-1",
        )

    async def test_route_plain_chat_delegates_to_chat_service(self) -> None:
        from app.services.routing.command_message_service import MessageContext

        router = self._build_router()
        ctx = MessageContext(
            raw={},
            content="plain chat",
            channel="channel",
            area="area",
            user="user-1",
            message_id="msg-1",
            timestamp="ts-1",
        )

        await router.route(ctx)

        self.chat.handle_plain_chat.assert_called_once_with("plain chat", "channel", "area")
        self.recall_scheduler.schedule_user_message_recall.assert_not_called()


class MentionCommandRouterTest(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_command_prefers_hint_over_ai_fallback(self) -> None:
        from app.services.routing.mention_command_router import MentionCommandRouter

        sender = Mock()
        plugins = Mock()
        plugins.try_dispatch_mention = AsyncMock(return_value=False)
        chat = AsyncMock()
        music = Mock()
        music.handle_mention = AsyncMock(return_value=False)
        help_service = Mock()
        help_service.suggest_commands.return_value = ["@bot 播放<歌名>"]

        runtime = _runtime(
            sender=sender,
            infrastructure=SimpleNamespace(sender=sender, plugins=plugins),
            plugins=plugins,
            plugin_host=object(),
            services=SimpleNamespace(
                interaction=SimpleNamespace(help=help_service, chat=chat, music=music),
                community=SimpleNamespace(member=Mock(), role=Mock(), target_resolution=Mock()),
                safety=SimpleNamespace(moderation=Mock(), recall=Mock()),
                scheduler=Mock(),
                plugins=SimpleNamespace(management=Mock()),
            ),
        )

        router = MentionCommandRouter(runtime)
        await router.dispatch("播发 稻香", "channel", "area", "user-1")

        chat.send_unknown_mention_command.assert_called_once()
        chat.handle_mention_fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
