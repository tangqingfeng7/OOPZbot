"""Web 播放器链接生成
"""

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import music.music_playback as playback  # noqa: E402


class WebPlayerLinkTest(unittest.IsolatedAsyncioTestCase):
    async def test_link_contains_the_resolved_token(self) -> None:
        with (
            patch.object(playback, "_get_web_player_url", return_value="https://example.test"),
            patch.object(playback, "ensure_token", new=AsyncMock(return_value="tok-123")),
        ):
            link = await playback._web_player_link(redis_client=object())

        self.assertEqual(link, "[▶ 网页播放器](https://example.test/w/tok-123)")

    async def test_link_never_embeds_a_coroutine(self) -> None:
        """回归本体：漏 await 时协程对象会被 str() 进 URL。"""
        with (
            patch.object(playback, "_get_web_player_url", return_value="https://example.test"),
            patch.object(playback, "ensure_token", new=AsyncMock(return_value="tok-123")),
        ):
            link = await playback._web_player_link(redis_client=object())

        self.assertNotIn("coroutine", link)
        self.assertNotIn("object at 0x", link)
        # token 段必须是普通的 URL 安全字符
        match = re.search(r"/w/([^)]+)\)$", link)
        assert match is not None
        self.assertRegex(match.group(1), r"^[A-Za-z0-9_.-]+$")

    async def test_falls_back_to_bare_url_without_token(self) -> None:
        with (
            patch.object(playback, "_get_web_player_url", return_value="https://example.test"),
            patch.object(playback, "ensure_token", new=AsyncMock(return_value="")),
        ):
            link = await playback._web_player_link()

        self.assertEqual(link, "[▶ 网页播放器](https://example.test)")

    async def test_no_url_configured_yields_empty_link(self) -> None:
        with patch.object(playback, "_get_web_player_url", return_value=""):
            self.assertEqual(await playback._web_player_link(), "")

    async def test_ttl_is_passed_through_to_token(self) -> None:
        ensure_token = AsyncMock(return_value="tok-1")
        with (
            patch.object(playback, "_get_web_player_url", return_value="https://example.test"),
            patch.object(playback, "ensure_token", new=ensure_token),
            patch.dict(playback.WEB_PLAYER_CONFIG, {"token_ttl_seconds": 3600}),
        ):
            await playback._web_player_link(redis_client=None)

        ensure_token.assert_awaited_once_with(redis_client=None, ttl_seconds=3600)

    async def test_illegal_ttl_falls_back_to_default(self) -> None:
        ensure_token = AsyncMock(return_value="tok-1")
        with (
            patch.object(playback, "_get_web_player_url", return_value="https://example.test"),
            patch.object(playback, "ensure_token", new=ensure_token),
            patch.dict(playback.WEB_PLAYER_CONFIG, {"token_ttl_seconds": "不是数字"}),
        ):
            await playback._web_player_link(redis_client=None)

        ensure_token.assert_awaited_once_with(redis_client=None, ttl_seconds=86400)


class GetWebLinkRedisClientTest(unittest.IsolatedAsyncioTestCase):
    """`_get_web_link` 取 Redis 客户端的方式也曾失效过。

    `QueueManager` 的入口是 `async client()`，旧的 `redis` 属性已不存在；
    继续用 `getattr(q, "redis", None)` 会恒得 None，令牌退化成不带 Redis 的
    进程内版本——多进程部署下每个进程各发各的令牌，互相不认。
    """

    async def test_redis_client_comes_from_queue_client(self) -> None:
        import music.music as music_module

        handler = music_module.MusicHandler.__new__(music_module.MusicHandler)
        redis_client = object()
        queue = Mock()
        queue.client = AsyncMock(return_value=redis_client)
        handler._get_queue = Mock(return_value=queue)
        handler._mark_web_active_area = AsyncMock()
        handler._web_link_released_due_to_idle = True

        link_factory = AsyncMock(return_value="[player](https://example.test)")
        with patch.object(music_module, "_web_player_link", new=link_factory):
            await handler._get_web_link(area="area-1")

        queue.client.assert_awaited_once()
        link_factory.assert_awaited_once_with(redis_client=redis_client)


if __name__ == "__main__":
    unittest.main()
