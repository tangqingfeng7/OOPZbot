from __future__ import annotations

import json
import time
import uuid
import random
import threading
import copy
from typing import Optional

from oopz.oopz_sender import OopzSender
from music.netease import NeteaseCloud
from core.queue_manager import QueueManager
from core.database import ImageCache, SongCache, Statistics
from oopz.name_resolver import NameResolver
from music.voice_client import VoiceClient
from config import OOPZ_CONFIG, WEB_PLAYER_CONFIG
from core.logger_config import get_logger
from web.web_link_token import clear_token, get_token, set_active_area
from music.music_web_control import WebControlExecutor
from music.music_platform import PlatformRegistry
from music.music_playback import (
    PlaybackMixin,
    reset_web_player_url_cache,  # noqa: F401 — re-export
    _web_player_link,
)

logger = get_logger("Music")

_LIKED_PER_PAGE = 20
_PLATFORM_NETEASE = "netease"
PLAY_MODE_LIST = "list"
PLAY_MODE_SINGLE = "single"
PLAY_MODE_SHUFFLE = "shuffle"
PLAY_MODE_AUTOPLAY = "autoplay"
_VALID_PLAY_MODES = {PLAY_MODE_LIST, PLAY_MODE_SINGLE, PLAY_MODE_SHUFFLE, PLAY_MODE_AUTOPLAY}

_PLATFORM_PREFIX_MAP = {
    "qq:": "qq",
    "QQ:": "qq",
    "qq\uff1a": "qq",
    "QQ\uff1a": "qq",
    "b\u7ad9:": "bilibili",
    "B\u7ad9:": "bilibili",
    "bili:": "bilibili",
    "BILI:": "bilibili",
    "b\u7ad9\uff1a": "bilibili",
    "B\u7ad9\uff1a": "bilibili",
    "bili\uff1a": "bilibili",
    "\u7f51\u6613:": "netease",
    "\u7f51\u6613\uff1a": "netease",
    "netease:": "netease",
    "netease\uff1a": "netease",
}


def parse_platform_prefix(keyword: str) -> tuple[str, str]:
    """从关键词中解析平台前缀，返回 (platform, clean_keyword)。"""
    for prefix, platform in _PLATFORM_PREFIX_MAP.items():
        if keyword.startswith(prefix):
            return platform, keyword[len(prefix):].strip()
    return "", keyword


def _music_auto_play_enabled() -> bool:
    try:
        from web.web_player_config import MUSIC_CONFIG
        return bool(MUSIC_CONFIG.get("auto_play_enabled", True))
    except Exception:
        return True


class MusicHandler(PlaybackMixin):
    """音乐功能处理器。
    队列按域隔离，每个域拥有独立的 QueueManager。
    语音连接同一时刻只有一个（Agora 限制），通过 _voice_channel_area 标识当前所在域。"""

    def __init__(self, sender: OopzSender, voice: Optional[VoiceClient] = None):
        self.supports_interactive_selection = True
        self.sender = sender
        self.voice = voice
        self.netease = NeteaseCloud()
        self._queue_cache: dict[str, QueueManager] = {}
        self.names = NameResolver()
        self._playback_lock = threading.Lock()
        self._liked_cache: list = []
        self._liked_ids_cache: list = []
        self._play_start_time: float = 0
        self._play_duration: float = 0
        self._voice_channel_id: Optional[str] = None
        self._voice_channel_area: Optional[str] = None
        self._voice_enter_time: float = 0
        self._playlist_idle_since: float = 0
        self._web_link_released_due_to_idle: bool = False
        self._web_control = WebControlExecutor(self)
        self.platforms = PlatformRegistry()
        self.platforms.register(self.netease)
        self._init_extra_platforms()

        # 登录账号"我喜欢的音乐"本地搜索索引：/bf 时优先在这里命中，
        # 命中可省一次 cloudsearch 且更贴近用户口味。索引在后台懒加载，
        # 不阻塞启动；30 分钟过期后下一次 /bf 会触发后台刷新。
        self._liked_search_index: list[dict] = []
        self._liked_search_loaded_at: float = 0.0
        self._liked_search_loading: bool = False
        self._liked_search_lock = threading.Lock()
        threading.Thread(
            target=self._refresh_liked_search_index, daemon=True,
        ).start()

        # 封面下载+上传 side channel：首次播放某首歌时，封面没缓存
        # 需要 1-3s 的"下载封面 + 申请 OSS 签名 + 上传"同步阻塞；
        # 通过在搜索成功后立即后台预热，使其与 voice 加入/队列写入并行，
        # 真正发消息时直接取结果，能省掉这段串行延迟。
        # song_data 要进 Redis 不能塞 Event，故用 (platform, song_id)
        # 作 key 的进程内注册表承载 future。
        self._cover_prefetch: dict[str, tuple[threading.Event, dict]] = {}
        self._cover_prefetch_lock = threading.Lock()

    def _get_queue(self, area: str = "") -> QueueManager:
        """获取域隔离的 QueueManager（带缓存）。"""
        area = (area or "").strip()
        if area not in self._queue_cache:
            self._queue_cache[area] = QueueManager(area=area)
        return self._queue_cache[area]

    @property
    def queue(self) -> QueueManager:
        """向后兼容：未传 area 时使用当前语音域的队列，或全局默认队列。"""
        override = getattr(self, "_queue_override", None)
        if override is not None:
            return override
        return self._get_queue(self._voice_channel_area or "")

    @queue.setter
    def queue(self, value) -> None:
        """兼容测试注入 mock queue 的旧写法。"""
        self._queue_override = value

    def _init_extra_platforms(self) -> None:
        """初始化并注册 QQ 音乐和 B 站平台（仅在配置启用时）。"""
        try:
            from music.qq_music import QQMusic
            qq = QQMusic()
            if qq.enabled:
                self.platforms.register(qq)
                logger.info("QQ 音乐平台已注册")
        except Exception as e:
            logger.debug("QQ 音乐平台初始化跳过: %s", e)
        try:
            from music.bilibili_music import BilibiliMusic
            bili = BilibiliMusic()
            if bili.enabled:
                self.platforms.register(bili)
                logger.info("B 站音乐平台已注册")
        except Exception as e:
            logger.debug("B 站音乐平台初始化跳过: %s", e)

    def refresh_platforms(self) -> dict:
        """后台配置变更后刷新平台实例，让新 Cookie 立即用于当前进程。"""
        self.netease = NeteaseCloud()
        self.platforms = PlatformRegistry()
        self.platforms.register(self.netease)
        self._init_extra_platforms()
        return {
            "available": True,
            "refreshed": True,
            "platforms": list(self.platforms.available.keys()),
        }

    def _get_web_link(self, area: str = "") -> str:
        """获取 Web 播放器链接（按需生成随机访问令牌）。"""
        q = self._get_queue(area)
        redis_client = getattr(q, "redis", None)
        link = _web_player_link(redis_client=redis_client)
        if link:
            self._web_link_released_due_to_idle = False
            set_active_area(area, redis_client=redis_client)
        return link

    def _release_web_link_if_needed(self):
        """播放列表长时间空闲后，释放随机 Web 访问链接。"""
        timeout = int(WEB_PLAYER_CONFIG.get("link_idle_release_seconds", 1800) or 0)
        if timeout <= 0:
            self._playlist_idle_since = 0
            self._web_link_released_due_to_idle = False
            return

        q = self.queue
        try:
            current = q.get_current()
            queue_length = q.get_queue_length()
        except Exception as e:
            logger.debug(f"读取播放队列状态失败，跳过链接释放检查: {e}")
            return

        if current is None and queue_length == 0 and not self._is_playing():
            if self._playlist_idle_since <= 0:
                self._playlist_idle_since = time.time()
                return
            if getattr(self, "_web_link_released_due_to_idle", False):
                return
            idle_for = time.time() - self._playlist_idle_since
            if idle_for >= timeout:
                try:
                    token = get_token(redis_client=q.redis)
                    if token:
                        clear_token(redis_client=q.redis)
                        logger.info("播放列表空闲超时，已释放 Web 播放器访问链接令牌")
                except Exception as e:
                    logger.debug(f"释放 Web 播放器链接令牌失败: {e}")
                self._web_link_released_due_to_idle = True
        else:
            self._playlist_idle_since = 0
            self._web_link_released_due_to_idle = False

    # ------------------------------------------------------------------
    # 公共命令
    # ------------------------------------------------------------------

    def _do_enter_voice(self, voice_channel_id: str, area: str) -> dict:
        """执行语音频道进入的核心流程：退出旧频道、API 进入、Agora 连接、更新状态。
        成功返回 enter_channel 的响应 dict，失败返回含 "error" 键的 dict。"""
        from_channel = self._voice_channel_id or ""
        from_area = self._voice_channel_area or ""

        if self._voice_channel_id and self._voice_channel_id != voice_channel_id:
            self._leave_current_voice_channel()

        # 处理服务端残留：本地无记录但服务端可能仍记着 bot 在某个语音频道（上次崩溃/重启留下）。
        # 这种情况下直接 enter_channel 会被服务端视为重复进入，不广播"成员加入"事件，其他客户端看不到 bot。
        # 这里先查询一次 bot 自己是否还挂在某个语音频道，若有则先 leave 一次。
        if not self._voice_channel_id:
            self._cleanup_stale_voice_membership(area)

        agora_pid = self.voice.agora_uid if self.voice and self.voice.available else ""
        self.sender.enter_area(area=area)
        data = self.sender.enter_channel(
            channel=voice_channel_id, area=area,
            channel_type="VOICE",
            from_channel=from_channel,
            from_area=from_area,
            pid=agora_pid,
        )
        if "error" in data:
            logger.warning(f"Bot 进入语音频道失败: {data['error']}")
            return data

        logger.info(f"Bot 已进入语音频道: {self.names.channel(voice_channel_id)}")
        self._join_agora_room(data)
        self._voice_channel_id = voice_channel_id
        self._voice_channel_area = area
        self._voice_enter_time = time.time()
        return data

    def _check_and_enter_voice_channel(self, user: str, channel: str, area: str) -> bool:
        """
        检查用户是否在语音频道，若在则 Bot 进入该频道。
        若用户不在任何语音频道，发送提示并返回 False。
        若 Bot 正在其他频道播放，拒绝切换并提示。
        """
        if not self.voice or not self.voice.available:
            self.sender.send_message(
                "语音推流功能未启用或初始化失败，无法播放音乐。",
                channel=channel, area=area,
            )
            return False

        voice_ch_id = self.sender.get_voice_channel_for_user(user, area=area)
        if not voice_ch_id:
            self.sender.send_message(
                "请先加入一个语音频道，Bot 会跟随你进入并放歌。",
                channel=channel, area=area,
            )
            return False

        # 已经在用户所在的语音频道：直接复用现有连接。重新走 _do_enter_voice
        # 会让浏览器侧的 agoraJoin 先调用 agoraLeave，把当前正在播放的 audioTrack
        if (
            self._voice_channel_id == voice_ch_id
            and self._voice_channel_area == area
        ):
            return True

        if (self._voice_channel_id and self._voice_channel_id != voice_ch_id
                and self._is_playing()):
            cur_ch_name = self.names.channel(self._voice_channel_id)
            self.sender.send_message(
                f"Bot 正在 {cur_ch_name} 播放中，请等播完或到该频道使用 /st 停止。",
                channel=channel, area=area,
            )
            return False

        data = self._do_enter_voice(voice_ch_id, area)
        if "error" in data:
            self.sender.send_message(
                f"进入语音频道失败: {data['error']}，请稍后再试。",
                channel=channel, area=area,
            )
            return False
        return True

    def enter_voice_channel(self, voice_channel_id: str, area: str) -> dict:
        if not self.voice or not self.voice.available:
            return {"error": "voice_unavailable"}

        voice_channel_id = (voice_channel_id or "").strip()
        if not voice_channel_id:
            return {"error": "missing_channel"}

        return self._do_enter_voice(voice_channel_id, area)

    def _join_agora_room(self, channel_data: dict):
        """使用 enter_channel 返回的凭证连接 Agora RTC。"""
        if not self.voice or not self.voice.available:
            return

        token = channel_data.get("supplierSign", "")
        room_id = channel_data.get("roomId", "")
        supplier = channel_data.get("supplier", "")
        logger.debug(f"enter_channel 返回: supplier={supplier}, "
                     f"roomId={room_id}, supplierSign={'有' if token else '空'}")
        if not token or not room_id:
            logger.warning("enter_channel 未返回 Agora 凭证，跳过 RTC 连接")
            return

        uid = int(self.voice.agora_uid)
        ok = self.voice.join(token=token, room_id=room_id, uid=uid)
        if ok:
            logger.info(f"Agora RTC 已连接: room={room_id}, uid={uid}")
            self._restore_volume_from_redis()
        else:
            logger.warning("Agora RTC 连接失败")

    def _restore_volume_from_redis(self) -> None:
        """从 Redis 恢复用户上次设置的播放音量。

        浏览器进程重启或重新加入 Agora 房间后，agora_player.html 内的
        `_currentVolume` 会回到默认值。这里把 Redis 持久化的音量回灌到浏览器，
        避免用户每次重连后都得手动重新拉一遍音量条。
        """
        if not self.voice or not self.voice.available:
            return
        try:
            raw = self.queue.redis.get("music:volume")
        except Exception as e:
            logger.debug(f"读取 music:volume 失败，跳过音量恢复: {e}")
            return
        if raw is None:
            return
        try:
            vol = int(raw)
        except (TypeError, ValueError):
            return
        vol = max(0, min(100, vol))
        try:
            self.voice.set_volume(vol)
            logger.debug(f"已从 Redis 恢复播放音量: {vol}")
        except Exception as e:
            logger.debug(f"恢复音量失败: {e}")

    def _cleanup_stale_voice_membership(self, area: str) -> None:
        """清理服务端残留的 bot 语音频道成员状态。

        bot 进程上次未正常退出时，服务端可能仍把 bot 挂在某个语音频道里。
        新一次 enter_channel 会被当作重复进入，不会广播"成员加入"事件，
        其他客户端因此看不到 bot。这里在进入前主动 leave 一次。
        """
        bot_uid = (OOPZ_CONFIG.get("person_uid") or "").strip()
        if not bot_uid:
            return
        try:
            stale_ch = self.sender.get_voice_channel_for_user(bot_uid, area=area)
        except Exception as e:
            logger.debug(f"查询服务端残留语音状态失败，跳过清理: {e}")
            return
        if not stale_ch:
            return
        logger.info(f"检测到服务端残留: bot 仍登记在语音频道 {stale_ch}，先 leave 清理")
        try:
            self.sender.leave_voice_channel(channel=stale_ch, area=area)
        except Exception as e:
            logger.warning(f"清理残留语音状态失败: {e}")

    def _leave_current_voice_channel(self):
        """退出 Bot 当前所在的语音频道。"""
        if not self._voice_channel_id:
            return

        # 先断开 Agora RTC 连接
        if self.voice and self.voice.available:
            self.voice.leave()

        result = self.sender.leave_voice_channel(
            channel=self._voice_channel_id,
            area=self._voice_channel_area,
        )
        if "error" in result:
            logger.warning(f"退出语音频道失败: {result['error']}")
        else:
            logger.info(f"Bot 已退出语音频道: {self.names.channel(self._voice_channel_id)}")
        self._voice_channel_id = None
        self._voice_channel_area = None

    def play_netease(self, keyword: str, channel: str, area: str, user: str) -> None:
        """搜索网易云并播放或加入队列"""
        self.play_song(keyword, "netease", channel, area, user)

    def search_candidates(self, keyword: str, platform: str = _PLATFORM_NETEASE, limit: int = 5) -> list[dict]:
        """返回歌曲候选列表，用于交互式选择。"""
        resolved_platform = platform or _PLATFORM_NETEASE
        p = self.platforms.get(resolved_platform)
        if not p:
            return []
        return p.search_many(keyword, limit=max(1, min(limit, 10)))

    def search_best_candidate(self, keyword: str, platform: str = _PLATFORM_NETEASE) -> Optional[dict]:
        """快速搜索首条候选，用于 /bf 直播放歌的快速命中。

        netease 平台优先在登录账号"我喜欢的音乐"里搜，命中就用喜欢列表的那一首；
        没命中再退回到全网 cloudsearch。
        """
        resolved_platform = platform or _PLATFORM_NETEASE
        p = self.platforms.get(resolved_platform)
        if not p:
            return None
        if resolved_platform == _PLATFORM_NETEASE:
            try:
                liked_hit = self._lookup_liked_song(keyword)
            except Exception as e:
                logger.debug("喜欢列表搜索异常 (%r): %s", keyword, e)
                liked_hit = None
            if liked_hit:
                logger.info(
                    "/bf 命中喜欢列表: keyword=%r → %s - %s (id=%s)",
                    keyword,
                    liked_hit.get("name"),
                    liked_hit.get("artists"),
                    liked_hit.get("id"),
                )
                return liked_hit
        try:
            return p.search(keyword, limit=1)
        except Exception as e:
            logger.debug("快速搜索候选失败 (%s): %s", resolved_platform, e)
            return None

    @staticmethod
    def _build_song_data_from_platform_data(
        data: dict,
        platform: str,
        song_id,
        channel: str,
        area: str,
        user: str,
    ) -> dict:
        """把平台返回的歌曲信息统一转换为内部 song_data。"""
        duration_ms = data.get("duration", 0) or data.get("duration_ms", 0) or 0
        duration_text = data.get("durationText", "") or data.get("duration", "")
        return {
            "platform": platform,
            "song_id": str(data.get("id") or data.get("mid") or song_id),
            "name": data["name"],
            "artists": data["artists"],
            "album": data.get("album", ""),
            "url": data["url"],
            "cover": data.get("cover"),
            "duration": duration_text,
            "duration_ms": duration_ms,
            "attachments": [],
            "channel": channel,
            "area": area,
            "user": user,
        }

    def play_song_choice(self, song: dict, channel: str, area: str, user: str) -> None:
        """播放用户从候选列表中选中的歌曲。"""
        platform = song.get("platform") or _PLATFORM_NETEASE
        p = self.platforms.get(platform)
        if not p:
            self.sender.send_message(f"错误: 未知或未启用的音乐平台: {platform}", channel=channel, area=area)
            return
        song_id = song.get("id") or song.get("song_id") or song.get("mid")
        if not song_id:
            self.sender.send_message("错误: 无法识别歌曲 ID", channel=channel, area=area)
            return

        data = None
        if song.get("url"):
            data = dict(song)
        elif song.get("name") and song.get("artists"):
            try:
                url = p.get_song_url(
                    song_id,
                    expected_duration_ms=song.get("duration", 0) or song.get("duration_ms", 0) or 0,
                    song_name=song.get("name", ""),
                )
            except TypeError:
                url = p.get_song_url(song_id)
            if not url:
                detail = getattr(p, "last_song_url_error", "") or "无法获取播放链接"
                self.sender.send_message(f"错误: {detail}", channel=channel, area=area)
                return
            data = dict(song)
            data["url"] = url
        else:
            result = p.summarize_by_id(song_id)
            if result["code"] != "success":
                self.sender.send_message(f"错误: {result['message']}", channel=channel, area=area)
                return
            data = result["data"]

        song_data = self._build_song_data_from_platform_data(data, platform, song_id, channel, area, user)
        self._kickoff_cover_prefetch(song_data)
        if not self._check_and_enter_voice_channel(user, channel, area):
            return
        user_name = self.names.user(user) if user else "未知用户"
        result = self._commit_song_request(song_data, prefix=f"{user_name} 从搜歌结果中选择了")
        self.sender.send_message(text=result["message"], attachments=result.get("attachments", []), channel=channel, area=area)

    def play_song(self, keyword: str, platform: str, channel: str, area: str, user: str) -> None:
        """通用的多平台点歌入口。"""
        voice_ok = [None]
        voice_done = threading.Event()

        def _voice_check():
            voice_ok[0] = self._check_and_enter_voice_channel(user, channel, area)
            voice_done.set()

        threading.Thread(target=_voice_check, daemon=True).start()

        result = self._prepare_song_request(keyword, channel, area, user, platform=platform)
        if result["code"] != "success":
            self.sender.send_message(f"错误: {result['message']}", channel=channel, area=area)
            return

        voice_done.wait()
        if not voice_ok[0]:
            return

        result = self._commit_song_request(result["song_data"])

        text = result["message"]
        attachments = result.get("attachments", [])
        self.sender.send_message(text=text, attachments=attachments, channel=channel, area=area)

    def play_next(self, channel: str, area: str, user: str) -> None:
        """播放队列中的下一首"""
        if not self._voice_channel_id:
            self.sender.send_message(
                "Bot 当前不在语音频道，请先用 /bf 点歌或让 Bot 跟随进入语音频道。",
                channel=channel,
                area=area,
            )
            return

        with self._playback_lock:
            q = self._get_queue(area)
            next_song = q.play_next()
            if not next_song:
                self.sender.send_message("队列为空，没有下一首了", channel=channel, area=area)
                return

            next_song["channel"] = channel
            next_song["area"] = area

            if self.voice and self.voice.available:
                self.voice.stop_audio()
            self._play_start_time = 0
            self._play_duration = 0

            play_uuid = str(uuid.uuid4())
            next_song["play_uuid"] = play_uuid
            self._start_playing(next_song.get("duration_ms", 0))
            q.set_current(next_song)

            SongCache.record_play(
                song_id=next_song.get("song_id"),
                platform=next_song.get("platform"),
                data=next_song,
                channel_id=channel,
                user_id=user,
            )
            Statistics.update_today(next_song.get("platform", _PLATFORM_NETEASE), cache_hit=False)

            threading.Thread(
                target=self._stream_to_voice_channel,
                args=(next_song["url"], next_song.get("name", "music"), channel, area,
                      str(next_song.get("song_id", "")), next_song.get("duration_ms", 0)),
                daemon=True,
            ).start()
            self._preload_next_song_if_any()

        text = self._build_now_playing_text("切换到下一首", next_song)
        attachments = next_song.get("attachments", [])
        self.sender.send_message(text=text, attachments=attachments, channel=channel, area=area)

    def show_queue(self, channel: str, area: str) -> None:
        """显示当前队列"""
        q = self._get_queue(area)
        queue_list = q.get_queue(0, 9)
        if queue_list:
            total = q.get_queue_length()
            lines = [f"{i}. {s['name']} - {s.get('artists', '未知')}" for i, s in enumerate(queue_list, 1)]
            msg = "当前队列（前10首）:\n" + "\n".join(lines) + f"\n\n总计: {total} 首"
            self.sender.send_message(msg, channel=channel, area=area)
        else:
            self.sender.send_message("队列为空", channel=channel, area=area)

    def show_liked_list(self, channel: str, area: str, page: int = 1) -> None:
        """显示喜欢的音乐列表（每页 20 首）"""
        uid = self.netease.get_user_id()
        if not uid:
            self.sender.send_message("无法获取网易云账号信息，请检查 Cookie 是否过期", channel=channel, area=area)
            return

        # 刷新缓存
        if not self._liked_ids_cache:
            self._liked_ids_cache = self.netease.get_liked_ids(uid)

        if not self._liked_ids_cache:
            self.sender.send_message("你的喜欢列表为空", channel=channel, area=area)
            return

        total = len(self._liked_ids_cache)
        per_page = _LIKED_PER_PAGE
        total_pages = (total + per_page - 1) // per_page
        page = max(1, min(page, total_pages))

        start = (page - 1) * per_page
        end = min(start + per_page, total)
        page_ids = self._liked_ids_cache[start:end]

        # 批量获取歌曲详情
        details = self.netease.get_song_details_batch(page_ids)
        if not details:
            self.sender.send_message("获取歌曲信息失败，请稍后再试", channel=channel, area=area)
            return

        # 缓存当前页供 play_liked_by_index 使用
        self._liked_cache = details

        lines = [f"喜欢的音乐 (第 {page}/{total_pages} 页，共 {total} 首):"]
        for i, song in enumerate(details, start + 1):
            lines.append(f"  {i}. {song['name']} - {song['artists']}  [{song['durationText']}]")

        lines.append(f"\n用法: /like play <编号> 播放指定歌曲")
        lines.append(f"      /like list <页码> 翻页")

        self.sender.send_message("\n".join(lines), channel=channel, area=area)

    def play_liked_by_index(self, index: int, channel: str, area: str, user: str) -> None:
        """通过列表编号播放喜欢的歌曲"""
        if not self._check_and_enter_voice_channel(user, channel, area):
            return
        if not self._liked_ids_cache:
            self.sender.send_message("请先使用 /like list 查看列表", channel=channel, area=area)
            return

        total = len(self._liked_ids_cache)
        if index < 1 or index > total:
            self.sender.send_message(f"编号超出范围，请输入 1-{total}", channel=channel, area=area)
            return

        song_id = self._liked_ids_cache[index - 1]
        song_data = self._fetch_netease_song_data(song_id, channel, area, user)
        if not song_data:
            self.sender.send_message("获取歌曲失败，请稍后再试", channel=channel, area=area)
            return
        self._kickoff_cover_prefetch(song_data)

        user_name = self.names.user(user) if user else "未知用户"
        result = self._commit_song_request(song_data, prefix=f"{user_name} 从喜欢列表点播了")
        self.sender.send_message(
            text=result["message"],
            attachments=result.get("attachments", []),
            channel=channel,
            area=area,
        )

    def play_liked(self, channel: str, area: str, user: str, count: int = 1) -> None:
        """从登录账号的喜欢列表中随机选歌播放"""
        if not self._check_and_enter_voice_channel(user, channel, area):
            return
        uid = self.netease.get_user_id()
        if not uid:
            self.sender.send_message("无法获取网易云账号信息，请检查 Cookie 是否过期", channel=channel, area=area)
            return

        liked_ids = self.netease.get_liked_ids(uid)
        if not liked_ids:
            self.sender.send_message("你的喜欢列表为空", channel=channel, area=area)
            return

        count = min(count, 20, len(liked_ids))
        selected = random.sample(liked_ids, count)

        success_count = 0
        first_text = None
        first_attachments = []

        user_name = self.names.user(user) if user else "未知用户"
        prefix = f"{user_name} 随机播放了喜欢的音乐"

        for song_id in selected:
            song_data = self._fetch_netease_song_data(song_id, channel, area, user)
            if not song_data:
                logger.warning(f"喜欢列表歌曲获取失败 (ID: {song_id})")
                continue

            if success_count == 0:
                self._kickoff_cover_prefetch(song_data)
                result = self._commit_song_request(song_data, prefix=prefix)
                first_text = result["message"]
                first_attachments = result.get("attachments", [])
            else:
                self._get_queue(area).add_to_queue(song_data)

            success_count += 1

        if success_count == 0:
            self.sender.send_message("随机选歌失败，请稍后再试", channel=channel, area=area)
            return

        if count > 1 and success_count > 1:
            first_text += f"\n(共 {success_count} 首已加入队列)"

        self.sender.send_message(text=first_text, attachments=first_attachments, channel=channel, area=area)

    def stop_play(self, channel: str, area: str) -> None:
        """停止播放并退出语音频道"""
        with self._playback_lock:
            self._play_start_time = 0
            self._play_duration = 0
            q = self._get_queue(area)
            q.clear_current()
            try:
                q.clear_play_state()
            except Exception as e:
                logger.debug(f"停止播放时清理 play_state 失败: {e}")
            if self.voice and self.voice.available:
                self.voice.stop_audio()
            self._leave_current_voice_channel()
        self.sender.send_message("已停止播放，Bot 已退出语音频道", channel=channel, area=area)

    def get_play_mode(self) -> str:
        """读取当前播放模式；未配置时默认列表循环。"""
        q = self.queue
        mode = q.get_play_mode() if hasattr(q, "get_play_mode") else None
        if mode not in _VALID_PLAY_MODES:
            mode = PLAY_MODE_LIST
            if hasattr(q, "set_play_mode"):
                q.set_play_mode(mode)
        return mode

    def set_play_mode(self, mode: str) -> None:
        """设置播放模式。"""
        if mode not in _VALID_PLAY_MODES:
            raise ValueError(f"无效播放模式: {mode}")
        if hasattr(self.queue, "set_play_mode"):
            self.queue.set_play_mode(mode)

    def _build_autoplay_song(self, current_song: dict | None) -> Optional[dict]:
        uid = self.netease.get_user_id()
        if not uid:
            return None
        if not self._liked_ids_cache:
            self._liked_ids_cache = self.netease.get_liked_ids(uid)
        if not self._liked_ids_cache:
            return None
        song_id = random.choice(self._liked_ids_cache)
        result = self.netease.summarize_by_id(song_id)
        if result["code"] != "success":
            return None
        data = result["data"]
        inherited = current_song or {}
        return {
            "platform": _PLATFORM_NETEASE,
            "song_id": str(song_id),
            "name": data["name"],
            "artists": data["artists"],
            "album": data.get("album", ""),
            "url": data["url"],
            "cover": data.get("cover"),
            "duration": data.get("durationText", ""),
            "duration_ms": data.get("duration", 0),
            "attachments": [],
            "channel": inherited.get("channel", ""),
            "area": inherited.get("area", ""),
            "user": inherited.get("user", ""),
        }

    _LIKED_SEARCH_TTL = 1800  # 30 分钟

    def _refresh_liked_search_index(self) -> None:
        """后台刷新喜欢列表索引；失败静默回退到原有全网搜索。"""
        if self._liked_search_loading:
            return
        self._liked_search_loading = True
        try:
            uid = self.netease.get_user_id()
            if not uid:
                logger.debug("未登录或获取 uid 失败，跳过喜欢列表索引加载")
                return
            details = self.netease.get_all_liked_song_details(uid)
            if not details:
                logger.debug("喜欢列表为空或拉取失败，跳过索引加载")
                return
            with self._liked_search_lock:
                self._liked_search_index = details
                self._liked_search_loaded_at = time.time()
            logger.info(f"喜欢列表索引已加载: {len(details)} 首")
        except Exception as e:
            logger.warning(f"加载喜欢列表索引失败: {e}")
        finally:
            self._liked_search_loading = False

    @staticmethod
    def _match_liked_in(songs: list[dict], keyword: str) -> Optional[dict]:
        """在给定歌曲列表里按 keyword 找最匹配的一首。

        匹配策略（按优先级降序）：
          1. 歌名完全相等（大小写/空白不敏感）
          2. 关键字是 "歌名" 的子串
          3. 关键字是 "歌名 - 歌手" 的子串
          4. 关键字按空格分词后全部命中 "歌名 + 歌手 + 专辑"
        多个候选优先取歌名长度更短的（更精确）。
        """
        if not keyword or not keyword.strip() or not songs:
            return None
        kw = keyword.strip().lower()
        kw_tokens = [t for t in kw.split() if t]

        exact: list[dict] = []
        prefix_name: list[dict] = []
        contains_full: list[dict] = []
        token_match: list[dict] = []
        for song in songs:
            name = (song.get("name") or "").strip().lower()
            artists = (song.get("artists") or "").strip().lower()
            album = (song.get("album") or "").strip().lower()
            if not name:
                continue
            if name == kw:
                exact.append(song)
                continue
            if kw in name:
                prefix_name.append(song)
                continue
            full = f"{name} - {artists}"
            if kw in full:
                contains_full.append(song)
                continue
            haystack = f"{name} {artists} {album}"
            if len(kw_tokens) > 1 and all(tok in haystack for tok in kw_tokens):
                token_match.append(song)

        for bucket in (exact, prefix_name, contains_full, token_match):
            if bucket:
                bucket.sort(key=lambda s: len((s.get("name") or "")))
                return bucket[0]
        return None

    def _lookup_liked_song(self, keyword: str) -> Optional[dict]:
        """在喜欢列表里找最匹配的一首；返回 None 表示未命中（外层走全网搜索）。

        - 先在本地 cache 里搜
        - 如果 miss，做一次"增量补漏"：拉最新 likelist 找出 cache 里没有的
          新增 ID，只对增量拉详情，再搜一次。这能覆盖用户启动 bot 后才
          点心的歌（cache 拍的是启动那一刻的快照）。
        """
        if not keyword or not keyword.strip():
            return None

        with self._liked_search_lock:
            index = list(self._liked_search_index)
            loaded_at = self._liked_search_loaded_at

        if loaded_at and time.time() - loaded_at > self._LIKED_SEARCH_TTL:
            threading.Thread(
                target=self._refresh_liked_search_index, daemon=True,
            ).start()

        hit = self._match_liked_in(index, keyword)
        if hit:
            return hit

        if not index:
            logger.debug("/bf 喜欢列表索引尚未就绪，跳过喜欢列表搜索")
            return None

        # 增量补漏：cache miss 时检查是否有新增 ID 可补
        return self._lookup_in_new_liked(keyword, {s.get("id") for s in index if s.get("id")})

    _NEW_LIKED_PROBE_LIMIT = 200

    def _lookup_in_new_liked(self, keyword: str, existing_ids: set) -> Optional[dict]:
        """从 likelist 里找 cache 还没收录的新增 ID，按需拉详情匹配。"""
        try:
            uid = self.netease.get_user_id()
            if not uid:
                return None
            all_ids = self.netease.get_liked_ids(uid)
            new_ids = [i for i in all_ids if i not in existing_ids]
            if not new_ids:
                logger.debug(
                    "/bf 喜欢列表 miss 且无新增 ID，回退全网搜索: keyword=%r 索引大小=%d",
                    keyword, len(existing_ids),
                )
                return None
            # 仅拉前 N 条，避免一次 /bf 触发数百次 API
            probe = new_ids[: self._NEW_LIKED_PROBE_LIMIT]
            details: list[dict] = []
            for i in range(0, len(probe), 50):
                chunk = probe[i:i + 50]
                got = self.netease.get_song_details_batch(chunk)
                if got:
                    details.extend(got)
            if not details:
                return None

            hit = self._match_liked_in(details, keyword)
            with self._liked_search_lock:
                self._liked_search_index.extend(details)
                if not self._liked_search_loaded_at:
                    self._liked_search_loaded_at = time.time()
            if hit:
                logger.info(
                    "/bf 喜欢列表增量补漏命中: 新增 %d 首 → %s - %s",
                    len(details), hit.get("name"), hit.get("artists"),
                )
            else:
                logger.debug(
                    "/bf 增量补漏后仍未命中: keyword=%r 新增 %d 首",
                    keyword, len(details),
                )
            return hit
        except Exception as e:
            logger.debug(f"增量喜欢列表查询失败: {e}")
            return None

    def _dequeue_next_song(self, natural_end: bool, current_song: dict | None) -> tuple[Optional[dict], str]:
        """根据播放模式决定下一首歌。"""
        mode = self.get_play_mode()
        if natural_end and mode == PLAY_MODE_SINGLE and current_song:
            return copy.deepcopy(current_song), PLAY_MODE_SINGLE
        if mode == PLAY_MODE_SHUFFLE and hasattr(self.queue, "pop_random"):
            return self.queue.pop_random(), "queue"
        next_song = self.queue.play_next()
        if next_song:
            return next_song, "queue"
        if natural_end and (mode == PLAY_MODE_AUTOPLAY or (
            mode == PLAY_MODE_LIST and _music_auto_play_enabled()
        )):
            return self._build_autoplay_song(current_song), PLAY_MODE_AUTOPLAY
        return None, mode

    # ------------------------------------------------------------------
    # 自动播放监控（在 main.py 中作为后台线程启动）
    # ------------------------------------------------------------------

    def _update_play_state_redis(self, **overrides):
        """更新 Redis 中的 play_state，支持暂停/恢复/跳转时的状态同步"""
        try:
            ps = {"start_time": self._play_start_time, "duration": self._play_duration}
            ps.update(overrides)
            self.queue.set_play_state(ps)
        except Exception as e:
            logger.debug(f"更新 play_state 失败: {e}")

    def start_web_command_listener(self) -> None:
        """启动独立线程，通过 BLPOP 实时监听 Web 控制命令（无延迟）"""
        def _listener():
            logger.info("Web 命令监听线程已启动 (BLPOP)")
            last_warn_at = 0.0
            while True:
                try:
                    result = self.queue.redis.blpop("music:web_commands", timeout=2)
                    if result:
                        _, cmd_raw = result
                        cmd = cmd_raw.decode() if isinstance(cmd_raw, bytes) else str(cmd_raw)
                        self._execute_web_command(cmd)
                except Exception as e:
                    now = time.time()
                    if now - last_warn_at >= 30:
                        logger.warning(f"Web 命令监听异常（30s 节流）: {e}")
                        last_warn_at = now
                    else:
                        logger.debug(f"Web 命令监听异常（抑制告警）: {e}")
                    time.sleep(1)

        t = threading.Thread(target=_listener, daemon=True)
        t.start()

    def _execute_web_command(self, cmd: str):
        """执行单条 Web 控制命令"""
        # 兼容测试中 __new__ 构造未执行 __init__ 的场景
        if not hasattr(self, "_web_control") or self._web_control is None:
            self._web_control = WebControlExecutor(self)
        self._web_control.execute(cmd)


    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _fetch_netease_song_data(self, song_id: int, channel: str, area: str, user: str) -> Optional[dict]:
        """通过歌曲 ID 获取详情并构建统一的 song_data 字典，失败返回 None。"""
        result = self.netease.summarize_by_id(song_id)
        if result["code"] != "success":
            return None
        data = result["data"]
        return {
            "platform": _PLATFORM_NETEASE,
            "song_id": str(song_id),
            "name": data["name"],
            "artists": data["artists"],
            "album": data["album"],
            "url": data["url"],
            "cover": data.get("cover"),
            "duration": data["durationText"],
            "duration_ms": data.get("duration", 0),
            "attachments": [],
            "channel": channel,
            "area": area,
            "user": user,
        }

    def _prepare_song_request(self, keyword: str, channel: str, area: str, user: str, platform: str = "") -> dict:
        """搜索歌曲并准备播放数据，但不提前进入语音频道。"""
        resolved_platform = platform or _PLATFORM_NETEASE
        p = self.platforms.get(resolved_platform)
        if not p:
            return {"code": "error", "message": f"未知或未启用的音乐平台: {resolved_platform}"}

        if resolved_platform == _PLATFORM_NETEASE:
            liked_hit = self._lookup_liked_song(keyword)
            if liked_hit:
                summarized = p.summarize_by_id(liked_hit["id"])
                if summarized["code"] == "success":
                    logger.info(
                        "/bf 命中喜欢列表: keyword=%r → %s - %s (id=%s)",
                        keyword,
                        summarized["data"].get("name"),
                        summarized["data"].get("artists"),
                        liked_hit["id"],
                    )
                    song_data = self._build_song_data_from_platform_data(
                        summarized["data"], resolved_platform, liked_hit["id"],
                        channel, area, user,
                    )
                    self._kickoff_cover_prefetch(song_data)
                    return {"code": "success", "song_data": song_data}
                logger.debug(
                    "喜欢列表命中但取详情失败，回退全网搜索: id=%s err=%s",
                    liked_hit["id"], summarized.get("message"),
                )

        search_result = p.summarize(keyword)
        if search_result["code"] != "success":
            return search_result

        data = search_result["data"]
        song_data = self._build_song_data_from_platform_data(
            data,
            resolved_platform,
            keyword,
            channel,
            area,
            user,
        )
        self._kickoff_cover_prefetch(song_data)

        return {"code": "success", "song_data": song_data}

    _COVER_PREFETCH_TIMEOUT = 5.0
    _COVER_PREFETCH_TTL = 60.0

    def _cover_prefetch_key(self, song_data: dict) -> Optional[str]:
        cover = song_data.get("cover")
        song_id = song_data.get("song_id")
        if not cover or not song_id:
            return None
        platform = song_data.get("platform", _PLATFORM_NETEASE)
        return f"{platform}:{song_id}"

    def _kickoff_cover_prefetch(self, song_data: dict) -> None:
        """后台预热封面下载+上传。可重复调用，相同歌曲只会发起一次。"""
        key = self._cover_prefetch_key(song_data)
        if not key:
            return
        # 兼容测试中 __new__ 跳过 __init__ 的场景
        if not hasattr(self, "_cover_prefetch_lock"):
            return
        snapshot = dict(song_data)

        with self._cover_prefetch_lock:
            if key in self._cover_prefetch:
                return
            done = threading.Event()
            holder: dict = {}
            self._cover_prefetch[key] = (done, holder)

        def _task():
            try:
                holder["result"] = self._resolve_song_attachments(snapshot)
            except Exception as e:
                logger.debug(f"封面预热失败 ({key}): {e}")
            finally:
                done.set()
                # 没人来取就在 TTL 后清掉，避免长期占内存
                threading.Timer(
                    self._COVER_PREFETCH_TTL,
                    self._purge_cover_prefetch, args=(key,),
                ).start()

        threading.Thread(target=_task, daemon=True).start()

    def _consume_cover_prefetch(self, song_data: dict):
        """若该歌曲有正在进行/已完成的封面预热，等结果并取走；否则返回 None。"""
        key = self._cover_prefetch_key(song_data)
        if not key:
            return None
        if not hasattr(self, "_cover_prefetch_lock"):
            return None
        with self._cover_prefetch_lock:
            entry = self._cover_prefetch.pop(key, None)
        if not entry:
            return None
        done, holder = entry
        if not done.wait(self._COVER_PREFETCH_TIMEOUT):
            logger.debug(f"封面预热超时，回退同步处理: {key}")
            return None
        return holder.get("result")

    def _purge_cover_prefetch(self, key: str) -> None:
        with self._cover_prefetch_lock:
            self._cover_prefetch.pop(key, None)

    def _resolve_song_attachments(self, song_data: dict) -> tuple[list, Optional[int], bool]:
        """在真正提交播放前再处理封面，避免失败请求也触发上传和写库。"""
        attachments = list(song_data.get("attachments", []))
        image_cache_id = None
        cache_hit = False
        song_id = song_data.get("song_id")
        cover = song_data.get("cover")
        platform = song_data.get("platform", _PLATFORM_NETEASE)

        if not cover or not song_id:
            return attachments, image_cache_id, cache_hit

        cached = ImageCache.get_by_source(song_id, platform)
        if cached:
            attachments = [cached["attachment_data"]]
            image_cache_id = cached["id"]
            cache_hit = True
            ImageCache.increment_use(song_id, platform)
            return attachments, image_cache_id, cache_hit

        up = self.sender.upload_file_from_url(cover)
        if up.get("code") == "success":
            att = up["data"]
            attachments = [att]
            image_cache_id = ImageCache.save(song_id, platform, cover, att)
        return attachments, image_cache_id, cache_hit

    def _build_song_request_text(self, song_data: dict, prefix: str = "") -> str:
        """统一构建点歌通知文本。prefix 为空时使用默认的 'XXX 点播了' 格式。"""
        if not prefix:
            user_name = self.names.user(song_data.get("user", "")) if song_data.get("user") else "未知用户"
            prefix = f"{user_name} 点播了"

        platform_name = {
            "netease": "网易云",
            "qq": "QQ音乐",
            "bilibili": "B站",
        }.get(song_data.get("platform"), "网易云")

        text = (
            f"{prefix}:\n"
            f"来自于{platform_name}:\n"
            f"歌曲: {song_data['name']}\n"
            f"歌手: {song_data['artists']}\n"
            f"专辑: {song_data['album']}\n"
            f"时长: {song_data['duration']}"
        )

        link = self._get_web_link(area=song_data.get("area", ""))
        if link:
            text += f"\n{link}"

        attachments = song_data.get("attachments", [])
        if attachments:
            att = attachments[0]
            text = f"![IMAGEw{att['width']}h{att['height']}]({att['fileKey']})\n" + text
        return text

    def _commit_song_request(self, song_data: dict, prefix: str = "") -> dict:
        """将已准备好的歌曲请求正式提交为播放或排队。prefix 用于自定义通知前缀。"""
        song_data = dict(song_data)

        q = self._get_queue(song_data.get("area", ""))
        direct_play = False

        with self._playback_lock:
            is_playing = self._is_playing()
            current_song = q.get_current()
            queue_length = q.get_queue_length()

            if not is_playing and current_song is not None:
                logger.info("检测到残留状态: 歌曲已播完但 current 存在, 自动清理")
                q.clear_current()
                current_song = None

            if not is_playing and current_song is None and queue_length == 0:
                song_data = dict(song_data)
                play_uuid = str(uuid.uuid4())
                song_data["play_uuid"] = play_uuid
                self._start_playing(song_data.get("duration_ms", 0))
                q.set_current(song_data)

                threading.Thread(
                    target=self._stream_to_voice_channel,
                    args=(
                        song_data["url"],
                        song_data["name"],
                        song_data.get("channel", ""),
                        song_data.get("area", ""),
                        str(song_data.get("song_id", "")),
                        song_data.get("duration_ms", 0),
                    ),
                    daemon=True,
                ).start()
                self._preload_next_song_if_any()
                direct_play = True
            else:
                pos = q.add_to_queue(song_data)
                # 入队的歌也预加载，避免 /next 时浏览器去拉远程 URL 撞上
                # 5s 超时 → 回退本地 → 再上传，全程能给用户 7+s 的"假静默"。
                # 预加载 = 后台先把音频字节缓存到本机，next 时 voice.play_audio
                # 命中缓存直接 agoraPlayLocal，省掉远程超时和下载。
                self._preload_next_song_if_any()

        prefetched = self._consume_cover_prefetch(song_data)
        if prefetched is not None:
            attachments, image_cache_id, cache_hit = prefetched
        else:
            attachments, image_cache_id, cache_hit = self._resolve_song_attachments(song_data)
        song_data["attachments"] = attachments

        if direct_play:
            try:
                q.set_current(song_data)
            except Exception:
                pass
            SongCache.record_play(
                song_data.get("song_id"),
                song_data.get("platform", _PLATFORM_NETEASE),
                song_data,
                image_cache_id,
                song_data.get("channel", ""),
                song_data.get("user", ""),
            )
            Statistics.update_today(song_data.get("platform", _PLATFORM_NETEASE), cache_hit)
            text = self._build_song_request_text(song_data, prefix=prefix)
        else:
            actual = pos + 1 + (1 if current_song or is_playing else 0)
            text = self._build_song_request_text(song_data, prefix=prefix) + f"\n已加入队列 (位置: {actual})"

        return {"code": "success", "message": text, "attachments": attachments}
