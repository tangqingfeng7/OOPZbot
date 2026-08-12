"""服务端把 bot 踢出语音频道后，下次播放要重新进入
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from music.music import MusicHandler  # noqa: E402

BOT_UID = "bot-uid"
CHANNEL = "voice-1"
AREA = "area-1"


class VoiceMembershipRecheckTest(unittest.IsolatedAsyncioTestCase):
    """回归背景：Agora 只管音频，Oopz 的频道成员身份是另一套。

    成员身份掉了之后本地仍以为在频道里，于是不再重新进入，
    表现是听得到歌声却看不到 bot。
    """

    def setUp(self) -> None:
        self.handler: Any = MusicHandler.__new__(MusicHandler)
        self.handler._voice_channel_id = CHANNEL
        self.handler._voice_channel_area = AREA
        self.sender = Mock()
        self.handler.sender = self.sender

        import music.music as music_module

        self._orig_uid = music_module.OOPZ_CONFIG.get("person_uid")
        music_module.OOPZ_CONFIG["person_uid"] = BOT_UID
        self.addCleanup(
            music_module.OOPZ_CONFIG.__setitem__, "person_uid", self._orig_uid or ""
        )

    async def test_still_in_channel_when_server_agrees(self) -> None:
        self.sender.get_voice_channel_for_user_strict = AsyncMock(return_value=CHANNEL)

        self.assertTrue(await self.handler._still_registered_in_voice(CHANNEL, AREA))

    async def test_detects_membership_dropped_by_server(self) -> None:
        """核心：服务端说不在任何频道，本地就不能再认为还在。"""
        self.sender.get_voice_channel_for_user_strict = AsyncMock(return_value=None)

        self.assertFalse(
            await self.handler._still_registered_in_voice(CHANNEL, AREA),
            "成员身份掉了必须能察觉，否则永远不会重新进入，只剩音频没有人",
        )

    async def test_detects_being_moved_to_another_channel(self) -> None:
        self.sender.get_voice_channel_for_user_strict = AsyncMock(return_value="voice-9")

        self.assertFalse(await self.handler._still_registered_in_voice(CHANNEL, AREA))

    async def test_query_failure_keeps_current_state(self) -> None:
        """查询失败不能当成掉线，否则一次网络抖动就触发退出重进。"""
        self.sender.get_voice_channel_for_user_strict = AsyncMock(
            side_effect=RuntimeError("网络抖动")
        )

        self.assertTrue(await self.handler._still_registered_in_voice(CHANNEL, AREA))

    async def test_missing_bot_uid_keeps_current_state(self) -> None:
        import music.music as music_module

        music_module.OOPZ_CONFIG["person_uid"] = ""
        self.sender.get_voice_channel_for_user_strict = AsyncMock(return_value=None)

        self.assertTrue(await self.handler._still_registered_in_voice(CHANNEL, AREA))
        self.sender.get_voice_channel_for_user_strict.assert_not_awaited()


class StrictQueryContractTest(unittest.IsolatedAsyncioTestCase):
    """宽松版把「查询失败」和「不在频道」都返回 None，校验必须用严格版。"""

    async def test_strict_variant_propagates_errors(self) -> None:
        from oopz.sdk_gateway import AsyncOopzGateway

        gateway: Any = AsyncOopzGateway.__new__(AsyncOopzGateway)
        gateway.bot = Mock()
        gateway.bot.channels.get_voice_channel_for_user = AsyncMock(
            side_effect=RuntimeError("boom")
        )
        gateway._default_area = Mock(return_value=AREA)

        with self.assertRaises(RuntimeError):
            await gateway.get_voice_channel_for_user_strict(BOT_UID, area=AREA)

        # 返回与「不在频道」相同的 None
        self.assertIsNone(await gateway.get_voice_channel_for_user(BOT_UID, area=AREA))


if __name__ == "__main__":
    unittest.main()
