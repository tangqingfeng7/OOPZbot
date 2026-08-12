"""自动播放监控的等待逻辑：一首放完要立刻醒，不能白等一整个轮询周期
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from music.music_playback import PlaybackMixin  # noqa: E402


class _Waiter(PlaybackMixin):
    """只取 PlaybackMixin 的等待逻辑，不拉起整个音乐处理器。"""

    def __init__(self, voice: Any) -> None:
        self.voice = voice


class WaitNextCheckTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.voice = Mock()
        self.voice.playback_ended = asyncio.Event()
        self.waiter = _Waiter(self.voice)

    async def _elapsed(self, coro) -> tuple[float, Any]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await coro
        return loop.time() - started, result

    async def test_playback_end_wakes_immediately(self) -> None:
        stop = asyncio.Event()

        async def finish_soon() -> None:
            await asyncio.sleep(0.05)
            self.voice.playback_ended.set()

        asyncio.get_running_loop().create_task(finish_soon())
        elapsed, stopped = await self._elapsed(self.waiter._wait_next_check(stop, 10))

        self.assertFalse(stopped)
        self.assertLess(
            elapsed, 1.0, "播完必须立刻唤醒，等满轮询周期会让切歌出现明显空档"
        )

    async def test_stop_event_wakes_and_reports_stop(self) -> None:
        stop = asyncio.Event()

        async def stop_soon() -> None:
            await asyncio.sleep(0.05)
            stop.set()

        asyncio.get_running_loop().create_task(stop_soon())
        elapsed, stopped = await self._elapsed(self.waiter._wait_next_check(stop, 10))

        self.assertTrue(stopped, "收到停止信号要如实上报，让监控退出")
        self.assertLess(elapsed, 1.0)

    async def test_falls_back_to_timeout_when_nothing_happens(self) -> None:
        stop = asyncio.Event()
        elapsed, stopped = await self._elapsed(self.waiter._wait_next_check(stop, 0.1))

        self.assertFalse(stopped)
        self.assertGreaterEqual(elapsed, 0.1, "没有事件时仍按超时定期检查")

    async def test_works_when_voice_has_no_event(self) -> None:
        """语音后端不可用或替身没有该属性时，退回纯超时等待。"""
        waiter = _Waiter(Mock(spec=[]))
        stop = asyncio.Event()

        elapsed, stopped = await self._elapsed(waiter._wait_next_check(stop, 0.1))

        self.assertFalse(stopped)
        self.assertGreaterEqual(elapsed, 0.1)

    async def test_leaves_no_pending_waiters(self) -> None:
        stop = asyncio.Event()
        before = len(asyncio.all_tasks())

        await self.waiter._wait_next_check(stop, 0.05)
        await asyncio.sleep(0)

        self.assertLessEqual(
            len(asyncio.all_tasks()), before, "等待结束后不应残留未取消的等待任务"
        )


if __name__ == "__main__":
    unittest.main()
