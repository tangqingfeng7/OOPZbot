"""面向音乐业务的 Oopz-SDK 语音控制器。"""

from __future__ import annotations

import asyncio
import inspect
from collections import OrderedDict
from typing import Any

from core.http_constants import HTTP_TIMEOUT_DOWNLOAD
from core.logger_config import get_logger
from oopz.remote_fetch import SafeRemoteFetcher

logger = get_logger("SdkVoiceController")
MAX_AUDIO_BYTES = 100 * 1024 * 1024


class SdkVoiceController:
    """以 SDK Voice 为后端，补充安全下载、预加载和播放状态。"""

    def __init__(self, voice_service, *, proxy_value=None, supervisor=None) -> None:
        self._voice = voice_service
        self._fetcher = SafeRemoteFetcher(proxy_value=proxy_value)
        self._supervisor = supervisor
        self._preloaded: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self._preload_tasks: dict[str, asyncio.Task[None]] = {}
        self._playing = False
        self._closed = False

    @property
    def available(self) -> bool:
        return not self._closed

    async def warmup(self) -> None:
        if self._closed:
            return
        try:
            await self._voice.start()
        except Exception as exc:
            logger.warning("语音浏览器预热失败，首次进入语音频道可能变慢: %s", exc)

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def agora_uid(self) -> str:
        sign = self._voice.current_sign
        return str(getattr(sign, "agora_sign_pid", "") or "")

    async def join(
        self,
        *,
        area: str,
        channel: str,
        from_area: str = "",
        from_channel: str = "",
        rtc_uid: str | int | None = None,
    ):
        return await self._voice.join(
            area=area,
            channel=channel,
            from_area=from_area,
            from_channel=from_channel,
            rtc_uid=rtc_uid,
        )

    async def leave(self) -> None:
        self._playing = False
        await self._voice.leave()

    def preload_audio(self, url: str) -> None:
        url = str(url or "").strip()
        if not url or url in self._preloaded or url in self._preload_tasks:
            return
        coroutine = self._preload(url)
        task = (
            self._supervisor.create(coroutine, name="voice-audio-preload")
            if self._supervisor is not None
            else asyncio.create_task(coroutine, name="voice-audio-preload")
        )
        self._preload_tasks[url] = task
        task.add_done_callback(lambda _task, key=url: self._preload_tasks.pop(key, None))

    async def _preload(self, url: str) -> None:
        try:
            payload = await asyncio.to_thread(
                self._fetcher.fetch,
                url,
                max_bytes=MAX_AUDIO_BYTES,
                timeout=(10, HTTP_TIMEOUT_DOWNLOAD),
            )
        except Exception as exc:
            logger.debug("语音预加载失败: %s", exc)
            return
        self._preloaded[url] = payload
        self._preloaded.move_to_end(url)
        while len(self._preloaded) > 3:
            self._preloaded.popitem(last=False)

    async def play_audio(self, url: str, on_started=None) -> dict[str, Any]:
        url = str(url or "").strip()
        if not url:
            raise ValueError("音频 URL 不能为空")
        pending = self._preload_tasks.get(url)
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        cached = self._preloaded.pop(url, None)
        if cached is None:
            cached = await asyncio.to_thread(
                self._fetcher.fetch,
                url,
                max_bytes=MAX_AUDIO_BYTES,
                timeout=(10, HTTP_TIMEOUT_DOWNLOAD),
            )
        data, mime_type = cached
        result = await self._voice.play_bytes(data, mime_type=mime_type or "audio/mpeg")
        if not result or not result.get("ok", False):
            raise RuntimeError(str((result or {}).get("error") or "SDK 语音播放失败"))
        self._playing = True
        if on_started is not None:
            callback_result = on_started()
            if inspect.isawaitable(callback_result):
                await callback_result
        return result

    async def stop_audio(self) -> None:
        self._playing = False
        await self._voice.stop()

    async def pause_audio(self) -> bool:
        result = await self._voice.pause()
        if result:
            self._playing = False
        return result

    async def resume_audio(self) -> bool:
        result = await self._voice.resume()
        if result:
            self._playing = True
        return result

    async def seek_audio(self, seconds: float) -> bool:
        return await self._voice.seek(seconds)

    async def set_volume(self, volume: int) -> bool:
        return await self._voice.set_volume(volume)

    async def destroy(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        for task in tuple(self._preload_tasks.values()):
            task.cancel()
        if self._preload_tasks:
            await asyncio.gather(*self._preload_tasks.values(), return_exceptions=True)
        self._preload_tasks.clear()
        self._preloaded.clear()
        # 关停必须先退出语音频道：SDK 的 close() 只停身份心跳并关掉浏览器，
        # 不会调用 leave_voice_channel，服务端会继续把 bot 挂在频道里。
        # 残留的成员身份会让下一次 enter_channel 被当作「重复进入」——服务端
        # 不再广播成员加入事件，于是其他客户端听得到 bot 推流却看不到它在频道里。
        try:
            await asyncio.wait_for(self._voice.leave(), timeout=max(0.1, timeout))
        except Exception as exc:
            logger.warning("关停时退出语音频道失败，服务端可能残留成员状态: %s", exc)
        await asyncio.wait_for(self._voice.close(), timeout=max(0.1, timeout))


__all__ = ["MAX_AUDIO_BYTES", "SdkVoiceController"]
