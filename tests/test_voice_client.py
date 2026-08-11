import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from music.sdk_voice import SdkVoiceController  # noqa: E402
from oopz.sdk_transport import ProjectBrowserVoiceTransport  # noqa: E402


class SdkVoiceControllerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.voice = Mock()
        self.voice.current_sign = Mock(agora_sign_pid="123")
        self.voice.join = AsyncMock(return_value={"ok": True})
        self.voice.leave = AsyncMock()
        self.voice.play_bytes = AsyncMock(return_value={"ok": True})
        self.voice.stop = AsyncMock()
        self.voice.pause = AsyncMock(return_value=True)
        self.voice.resume = AsyncMock(return_value=True)
        self.voice.seek = AsyncMock(return_value=True)
        self.voice.set_volume = AsyncMock(return_value=True)
        self.voice.close = AsyncMock()
        self.controller = SdkVoiceController(self.voice, proxy_value=False)

    async def asyncTearDown(self) -> None:
        if not self.controller._closed:
            await self.controller.destroy()

    async def test_join_delegates_to_sdk_voice(self) -> None:
        await self.controller.join(
            area="area-1",
            channel="voice-1",
            from_area="area-0",
            from_channel="voice-0",
            rtc_uid=123,
        )
        self.voice.join.assert_awaited_once_with(
            area="area-1",
            channel="voice-1",
            from_area="area-0",
            from_channel="voice-0",
            rtc_uid=123,
        )

    async def test_preload_is_reused_by_playback(self) -> None:
        self.controller._fetcher.fetch = Mock(return_value=(b"audio", "audio/mpeg"))
        self.controller.preload_audio("https://example.com/song.mp3")
        await asyncio.gather(*self.controller._preload_tasks.values())

        await self.controller.play_audio("https://example.com/song.mp3")

        self.controller._fetcher.fetch.assert_called_once()
        self.voice.play_bytes.assert_awaited_once_with(b"audio", mime_type="audio/mpeg")
        self.assertTrue(self.controller.is_playing)

    async def test_playback_awaits_async_started_callback(self) -> None:
        self.controller._fetcher.fetch = Mock(return_value=(b"audio", "audio/ogg"))
        callback = AsyncMock()

        await self.controller.play_audio("https://example.com/song.ogg", callback)

        callback.assert_awaited_once_with()

    async def test_sdk_play_error_is_not_reported_as_success(self) -> None:
        self.controller._fetcher.fetch = Mock(return_value=(b"audio", "audio/mpeg"))
        self.voice.play_bytes.return_value = {"ok": False, "error": "decode failed"}

        with self.assertRaisesRegex(RuntimeError, "decode failed"):
            await self.controller.play_audio("https://example.com/song.mp3")
        self.assertFalse(self.controller.is_playing)

    async def test_destroy_is_idempotent_and_closes_sdk_backend(self) -> None:
        await self.controller.destroy()
        await self.controller.destroy()

        self.voice.close.assert_awaited_once_with()
        self.assertFalse(self.controller.available)


class SeleniumFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_when_playwright_initialization_fails(self) -> None:
        transport = ProjectBrowserVoiceTransport(Mock(), proxy_value=False)
        with (
            patch.object(
                transport,
                "_init_playwright_browser",
                AsyncMock(side_effect=RuntimeError("playwright failed")),
            ),
            patch.object(transport, "_init_selenium_browser") as selenium,
            patch(
                "oopz_sdk.transport.voice_browser.BrowserVoiceTransport._shutdown_browser",
                new=AsyncMock(),
            ),
        ):
            await transport._init_browser()

        selenium.assert_called_once_with()
        self.assertTrue(transport._init_done.is_set())

    async def test_does_not_start_selenium_when_playwright_succeeds(self) -> None:
        transport = ProjectBrowserVoiceTransport(Mock(), proxy_value=False)
        with (
            patch.object(transport, "_init_playwright_browser", new=AsyncMock()),
            patch.object(transport, "_init_selenium_browser") as selenium,
        ):
            await transport._init_browser()

        selenium.assert_not_called()


if __name__ == "__main__":
    unittest.main()
