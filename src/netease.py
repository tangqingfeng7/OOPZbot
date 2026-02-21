"""
网易云音乐 API 封装
依赖外部的网易云音乐 API 服务（如 NeteaseCloudMusicApi）
"""

import requests
from typing import Optional

from config import NETEASE_CLOUD
from logger_config import get_logger

logger = get_logger("Netease")


class NeteaseCloud:
    """网易云音乐搜索与获取"""

    def __init__(self):
        self.base_url = NETEASE_CLOUD.get("base_url", "").rstrip("/")
        self.cookie = NETEASE_CLOUD.get("cookie", "")
        if not self.base_url:
            logger.warning("网易云 API 地址未配置 (NETEASE_CLOUD.base_url)")

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        """发起 GET 请求"""
        if not self.base_url:
            return None
        try:
            headers = {}
            if self.cookie:
                headers["Cookie"] = self.cookie
            resp = requests.get(
                f"{self.base_url}{path}",
                params=params,
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"网易云 API 请求失败: {e}")
            return None

    def search(self, keyword: str, limit: int = 1) -> Optional[dict]:
        """
        搜索歌曲

        返回格式::
            {
                "id": 歌曲ID,
                "name": "歌名",
                "artists": "歌手",
                "album": "专辑",
                "duration": 毫秒,
                "cover": "封面URL"
            }
        """
        data = self._get("/cloudsearch", params={"keywords": keyword, "limit": limit, "type": 1})
        if not data or data.get("code") != 200:
            return None

        songs = data.get("result", {}).get("songs", [])
        if not songs:
            return None

        song = songs[0]
        artists = " / ".join(a["name"] for a in song.get("ar", []))
        album = song.get("al", {})
        duration_ms = song.get("dt", 0)

        return {
            "id": song["id"],
            "name": song["name"],
            "artists": artists,
            "album": album.get("name", ""),
            "duration": duration_ms,
            "durationText": self._format_duration(duration_ms),
            "cover": album.get("picUrl", ""),
        }

    def get_song_url(self, song_id: int) -> Optional[str]:
        """获取歌曲播放 URL"""
        data = self._get("/song/url/v1", params={"id": song_id, "level": "standard"})
        if not data or data.get("code") != 200:
            return None

        urls = data.get("data", [])
        if urls and urls[0].get("url"):
            return urls[0]["url"]
        return None

    def get_user_id(self) -> Optional[int]:
        """获取当前登录用户的 ID"""
        data = self._get("/user/account")
        if not data or data.get("code") != 200:
            return None
        profile = data.get("profile")
        return profile.get("userId") if profile else None

    def get_liked_ids(self, uid: int) -> list:
        """获取用户喜欢的歌曲 ID 列表"""
        data = self._get("/likelist", params={"uid": uid})
        if not data or data.get("code") != 200:
            return []
        return data.get("ids", [])

    def get_song_detail(self, song_id: int) -> Optional[dict]:
        """通过歌曲 ID 获取歌曲详细信息"""
        data = self._get("/song/detail", params={"ids": str(song_id)})
        if not data or data.get("code") != 200:
            return None

        songs = data.get("songs", [])
        if not songs:
            return None

        song = songs[0]
        artists = " / ".join(a["name"] for a in song.get("ar", []))
        album = song.get("al", {})
        duration_ms = song.get("dt", 0)

        return {
            "id": song["id"],
            "name": song["name"],
            "artists": artists,
            "album": album.get("name", ""),
            "duration": duration_ms,
            "durationText": self._format_duration(duration_ms),
            "cover": album.get("picUrl", ""),
        }

    def get_song_details_batch(self, song_ids: list) -> list:
        """批量获取歌曲详细信息（一次最多传 50 个 ID）"""
        if not song_ids:
            return []
        ids_str = ",".join(str(sid) for sid in song_ids)
        data = self._get("/song/detail", params={"ids": ids_str})
        if not data or data.get("code") != 200:
            return []

        results = []
        for song in data.get("songs", []):
            artists = " / ".join(a["name"] for a in song.get("ar", []))
            album = song.get("al", {})
            duration_ms = song.get("dt", 0)
            results.append({
                "id": song["id"],
                "name": song["name"],
                "artists": artists,
                "album": album.get("name", ""),
                "duration": duration_ms,
                "durationText": self._format_duration(duration_ms),
                "cover": album.get("picUrl", ""),
            })
        return results

    def summarize_by_id(self, song_id: int) -> dict:
        """通过歌曲 ID 获取完整信息（详情 + URL）"""
        song_info = self.get_song_detail(song_id)
        if not song_info:
            return {"code": "error", "message": f"无法获取歌曲信息: {song_id}", "data": None}

        url = self.get_song_url(song_id)
        if not url:
            return {"code": "error", "message": f"无法获取播放链接: {song_info['name']}", "data": None}

        song_info["url"] = url
        return {"code": "success", "message": "", "data": song_info}

    def summarize(self, keyword: str) -> dict:
        """
        搜索并汇总歌曲信息（搜索 + 获取 URL），
        返回统一格式供 music.py 调用。
        """
        song_info = self.search(keyword)
        if not song_info:
            return {"code": "error", "message": f"未找到: {keyword}", "data": None}

        url = self.get_song_url(song_info["id"])
        if not url:
            return {"code": "error", "message": f"无法获取播放链接: {song_info['name']}", "data": None}

        song_info["url"] = url

        msg = (
            f"歌曲: {song_info['name']}\n"
            f"歌手: {song_info['artists']}\n"
            f"专辑: {song_info['album']}\n"
            f"时长: {song_info['durationText']}"
        )
        return {"code": "success", "message": msg, "data": song_info}

    def get_lyric(self, song_id: int) -> Optional[str]:
        """获取歌曲 LRC 歌词文本，无歌词返回 None。"""
        data = self._get("/lyric/new", params={"id": song_id})
        if not data or data.get("code") != 200:
            return None
        lrc = data.get("lrc", {})
        lyric_text = lrc.get("lyric", "")
        return lyric_text if lyric_text and "[" in lyric_text else None

    @staticmethod
    def _format_duration(ms: int) -> str:
        s = ms // 1000
        return f"{s // 60}:{s % 60:02d}"
