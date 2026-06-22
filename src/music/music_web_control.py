import json
import threading
import time
import uuid

from core.database import SongCache, Statistics
from core.logger_config import get_logger
from core.redis_keys import VOLUME as KEY_VOLUME

logger = get_logger("MusicWebControl")


class WebControlExecutor:
    """执行来自 Web 面板的控制命令。"""

    def __init__(self, handler):
        self.h = handler

    def execute(self, cmd: str):
        logger.info(f"Web 控制命令: {cmd}")
        try:
            if cmd == "next":
                self._handle_next()
                return
            if cmd == "stop":
                self._handle_stop()
                return
            if cmd == "pause":
                self._handle_pause()
                return
            if cmd == "resume":
                self._handle_resume()
                return
            if cmd.startswith("seek:"):
                self._handle_seek(cmd)
                return
            if cmd.startswith("volume:"):
                self._handle_volume(cmd)
                return
            if cmd.startswith("notify:"):
                self._handle_notify(cmd)
                return
        except Exception as e:
            logger.warning(f"执行 Web 命令异常 ({cmd}): {e}")

    def _stop_voice_audio(self, context: str):
        if not (self.h.voice and self.h.voice.available):
            return
        try:
            self.h.voice.stop_audio()
        except Exception as e:
            logger.debug(f"{context} 停止音频失败: {e}")

    def _handle_next(self):
        """Web 端用户主动切下一首：直接完成出队 + 切歌 + 推流。

        这里不能依赖 auto_play_monitor 的轮询触发：
        - 监控线程会把残留的 current 视作"自然播完"，传 natural_end=True
          + current_song=旧歌 给 _dequeue_next_song，导致单曲循环重播本首；
        - 监控线程的轮询周期最长 10s，会让 UI 卡在"上一首"几秒钟。

        改成这里直接走切歌流程：
        - 通过 _dequeue_next_song(natural_end=True, current_song=None)
          既跳过 SINGLE 自循环（SINGLE 分支需要 current_song 才生效），
          又保留空队列时 AUTOPLAY 模式自动续播的能力。
        """
        h = self.h
        channel = h._voice_channel_id
        area = h._voice_channel_area or ""

        if not channel:
            h._play_start_time = 0
            h._play_duration = 0
            self._stop_voice_audio("执行 next 时")
            try:
                h.queue.clear_current()
                h.queue.clear_play_state()
            except Exception as e:
                logger.debug(f"未在语音频道时清理 play_state 失败: {e}")
            return

        next_song = None
        with h._playback_lock:
            try:
                next_song, _source = h._dequeue_next_song(
                    natural_end=True, current_song=None,
                )
            except Exception as e:
                logger.warning(f"Web next 取下一首失败: {e}")
                next_song = None

            self._stop_voice_audio("执行 next 时")
            h._play_start_time = 0
            h._play_duration = 0

            if not next_song:
                try:
                    h.queue.clear_current()
                    h.queue.clear_play_state()
                except Exception as e:
                    logger.debug(f"队列空清理 play_state 失败: {e}")
                return

            next_song["channel"] = next_song.get("channel") or channel
            next_song["area"] = next_song.get("area") or area
            next_song["play_uuid"] = str(uuid.uuid4())

            if hasattr(h, "_mark_web_active_area"):
                h._mark_web_active_area(next_song["area"])
            h._start_playing(next_song.get("duration_ms", 0), area=next_song["area"])
            h.queue.set_current(next_song)

            try:
                SongCache.record_play(
                    song_id=next_song.get("song_id"),
                    platform=next_song.get("platform"),
                    data=next_song,
                    channel_id=next_song["channel"],
                    user_id=next_song.get("user", ""),
                )
                Statistics.update_today(
                    next_song.get("platform", "netease"), cache_hit=False,
                )
            except Exception as e:
                logger.debug(f"记录 Web 切歌播放历史失败: {e}")

            threading.Thread(
                target=h._stream_to_voice_channel,
                args=(
                    next_song["url"],
                    next_song.get("name", "music"),
                    next_song["channel"],
                    next_song["area"],
                    str(next_song.get("song_id", "")),
                    next_song.get("duration_ms", 0),
                ),
                daemon=True,
            ).start()
            h._preload_next_song_if_any()

        try:
            text = h._build_now_playing_text("切换到下一首", next_song)
            h.sender.send_message(
                text=text,
                attachments=next_song.get("attachments", []),
                channel=next_song["channel"],
                area=next_song["area"],
            )
        except Exception as e:
            logger.warning(f"Web 切歌通知发送失败: {e}")

    def _handle_stop(self):
        self.h._play_start_time = 0
        self.h._play_duration = 0
        self.h.queue.clear_current()
        self.h.queue.clear_queue()
        try:
            self.h.queue.clear_play_state()
        except Exception as e:
            logger.debug(f"执行 stop 时清理 play_state 失败: {e}")
        self._stop_voice_audio("执行 stop 时")
        self.h._leave_current_voice_channel()

    def _handle_pause(self):
        if self.h.voice and self.h.voice.available and self.h.voice.pause_audio():
            elapsed = time.time() - self.h._play_start_time
            self.h._update_play_state_redis(paused=True, pause_elapsed=elapsed)

    def _handle_resume(self):
        if not (self.h.voice and self.h.voice.available and self.h.voice.resume_audio()):
            return
        try:
            ps = self.h.queue.get_play_state()
            if not ps:
                return
            elapsed = ps.get("pause_elapsed", 0)
            self.h._play_start_time = time.time() - elapsed
            self.h._update_play_state_redis(
                start_time=self.h._play_start_time,
                paused=False,
                pause_elapsed=None,
            )
        except Exception as e:
            logger.debug(f"执行 resume 时读取 play_state 失败: {e}")

    def _handle_seek(self, cmd: str):
        try:
            seek_time = float(cmd.split(":", 1)[1])
        except (ValueError, IndexError) as e:
            logger.debug(f"解析 seek 命令失败 ({cmd}): {e}")
            return
        if not (self.h.voice and self.h.voice.available):
            return
        if not self.h.voice.seek_audio(seek_time):
            return
        was_paused = False
        try:
            ps = self.h.queue.get_play_state()
            if ps:
                was_paused = bool(ps.get("paused"))
        except Exception:
            pass
        self.h._play_start_time = time.time() - seek_time
        self.h._update_play_state_redis(
            start_time=self.h._play_start_time,
            paused=was_paused,
            pause_elapsed=seek_time if was_paused else None,
        )

    def _handle_volume(self, cmd: str):
        try:
            vol = int(cmd.split(":", 1)[1])
        except (ValueError, IndexError) as e:
            logger.debug(f"解析 volume 命令失败 ({cmd}): {e}")
            return
        vol = max(0, min(100, vol))
        try:
            self.h.queue.redis.set(KEY_VOLUME, str(vol))
        except Exception as e:
            logger.debug(f"持久化音量失败: {e}")
        if not (self.h.voice and self.h.voice.available):
            return
        if not self.h.voice.set_volume(vol):
            return
        try:
            self.h.queue.redis.set(KEY_VOLUME, str(vol))
        except Exception as e:
            logger.debug(f"持久化音量失败: {e}")

    def _handle_notify(self, cmd: str):
        try:
            info = json.loads(cmd.split(":", 1)[1])
        except (ValueError, TypeError, IndexError, json.JSONDecodeError) as e:
            logger.debug(f"解析 notify 命令失败 ({cmd}): {e}")
            return

        ch = self.h._voice_channel_id
        ar = self.h._voice_channel_area
        if not ch:
            return

        name = info.get("name", "未知")
        artists = info.get("artists", "未知")
        pos = info.get("position", "?")
        try:
            pos_int = int(pos)
        except (ValueError, TypeError):
            pos_int = 1
        has_current = False
        try:
            has_current = self.h.queue.get_current() is not None
        except Exception as e:
            logger.debug(f"读取当前播放状态失败，按队列位置展示: {e}")
        if not has_current and hasattr(self.h, "_is_playing"):
            try:
                has_current = bool(self.h._is_playing())
            except Exception as e:
                logger.debug(f"读取播放状态失败，按队列位置展示: {e}")
        actual = max(1, pos_int + (1 if has_current else 0))
        text = f"[Web 点歌] {name} - {artists}\n已加入队列 (位置: {actual})"
        try:
            self.h.sender.send_message(text, channel=ch, area=ar)
        except Exception as e:
            logger.warning(f"Web 通知消息发送失败: {e}")
