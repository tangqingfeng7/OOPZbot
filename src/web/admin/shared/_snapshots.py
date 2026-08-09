"""后台总览与播放快照辅助：概览载荷、日志尾部、热门歌曲与队列快照。"""

from __future__ import annotations

import os
import time
from collections import deque

from core.database import MessageStatsDB, Statistics, db_connection
from core.logger_config import get_logger
from core.queue_manager import KEY_CURRENT, KEY_PLAY_STATE, KEY_QUEUE, _area_key
from core.redis_protocol import RedisDataStore, redis_json_object

from ._area import _music_area_context
from ._runtime import _get_redis, _get_started_at

logger = get_logger("WebPlayerAdmin")


def _overview_payload() -> dict:
    redis_status = "connected"
    queue_len = 0
    playing: dict = {}
    area_context = _music_area_context()
    try:
        r = _get_redis()
        r.ping()
        area_context = _music_area_context(r)
        area = area_context.get("area", "")
        if area:
            queue_len = r.llen(_area_key(KEY_QUEUE, area))
            current_raw = r.get(_area_key(KEY_CURRENT, area))
            play_state_raw = r.get(_area_key(KEY_PLAY_STATE, area))
            playing = {
                "available": True,
                "current": (
                    redis_json_object(current_raw, field="当前播放歌曲")
                    if current_raw
                    else None
                ),
                "play_state": (
                    redis_json_object(play_state_raw, field="播放状态")
                    if play_state_raw
                    else None
                ),
                "area": area,
            }
        else:
            playing = {
                "available": False,
                "code": "playback_area_unavailable",
                "current": None,
                "play_state": None,
                "area": "",
            }
    except Exception as e:
        redis_status = f"error: {e}"

    today = Statistics.get_today() or {}
    summary = Statistics.get_summary()
    return {
        "ok": True,
        "uptime_seconds": int(time.time() - _get_started_at()),
        "redis": redis_status,
        "queue_length": queue_len,
        "playing": playing,
        "music_area": area_context,
        "statistics_today": today,
        "statistics_summary": summary,
        "today_messages": MessageStatsDB.get_today_total(),
        "active_users_today": MessageStatsDB.get_active_users_today(),
    }


def _tail_file(path: str, lines: int = 200) -> list[str]:
    if not os.path.exists(path):
        return []
    max_lines = max(1, min(int(lines), 2000))
    dq: deque[str] = deque(maxlen=max_lines)
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            dq.append(line.rstrip("\n"))
    return list(dq)


def _top_songs_from_play_history(page: int = 1, page_size: int = 10) -> tuple[list[dict], int]:
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 10), 100))
    offset = (page - 1) * page_size
    with db_connection() as conn:
        total_row = conn.execute(
            """
            SELECT COUNT(1) AS c
            FROM (
                SELECT sc.song_id
                FROM play_history ph
                LEFT JOIN song_cache sc ON sc.id = ph.song_cache_id
                GROUP BY sc.song_id, sc.song_name, sc.artist, sc.album
            ) t
            """
        ).fetchone()
        total = int(total_row["c"] if total_row else 0)
        rows = conn.execute(
            """
            SELECT
                sc.song_id AS song_id,
                COALESCE(sc.song_name, '') AS song_name,
                COALESCE(sc.artist, '') AS artist,
                COALESCE(sc.album, '') AS album,
                COUNT(ph.id) AS play_count,
                MAX(ph.played_at) AS last_played_at
            FROM play_history ph
            LEFT JOIN song_cache sc ON sc.id = ph.song_cache_id
            GROUP BY sc.song_id, sc.song_name, sc.artist, sc.album
            ORDER BY play_count DESC, last_played_at DESC
            LIMIT ? OFFSET ?
            """,
            (page_size, offset),
        ).fetchall()
    return [dict(r) for r in rows], total


def _queue_snapshot(redis_client: RedisDataStore, area: str = "") -> list[dict]:
    if not str(area or "").strip():
        return []
    items = redis_client.lrange(_area_key(KEY_QUEUE, area), 0, -1)
    queue: list[dict] = []
    for i, item in enumerate(items):
        try:
            song = redis_json_object(item, field="队列歌曲")
        except Exception as e:
            logger.debug("解析队列项 %d 失败: %s", i, e)
            song = {}
        queue.append({
            "index": i,
            "id": song.get("song_id") or song.get("id"),
            "name": song.get("name", ""),
            "artists": song.get("artists", ""),
            "album": song.get("album", ""),
            "durationText": song.get("durationText") or song.get("duration", ""),
        })
    return queue


def _current_song_snapshot(
    redis_client: RedisDataStore,
    area: str = "",
) -> dict | None:
    if not str(area or "").strip():
        return None
    try:
        raw = redis_client.get(_area_key(KEY_CURRENT, area))
        if not raw:
            return None
        song = redis_json_object(raw, field="当前播放歌曲")
        return {
            "id": song.get("song_id") or song.get("id"),
            "name": song.get("name", ""),
            "artists": song.get("artists", ""),
            "album": song.get("album", ""),
            "durationText": song.get("durationText") or song.get("duration", ""),
        }
    except Exception:
        logger.debug("读取当前播放信息失败", exc_info=True)
        return None


__all__ = [
    "_current_song_snapshot",
    "_overview_payload",
    "_queue_snapshot",
    "_tail_file",
    "_top_songs_from_play_history",
]
