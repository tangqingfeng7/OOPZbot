"""
Web 歌词播放器 — FastAPI 服务
由 main.py 在后台线程启动，端口 8080
"""

import json
import os
import time
from threading import Lock
from typing import Optional

import redis
import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from config import REDIS_CONFIG
from logger_config import get_logger
from netease import NeteaseCloud

logger = get_logger("WebPlayer")

app = FastAPI(title="Oopz Music Player", docs_url=None, redoc_url=None)

_redis: Optional[redis.Redis] = None
_netease: Optional[NeteaseCloud] = None

_lyric_cache: dict[int, Optional[str]] = {}
_lyric_lock = Lock()
_LYRIC_CACHE_MAX = 200


def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis(**REDIS_CONFIG)
    return _redis


def _get_netease() -> NeteaseCloud:
    global _netease
    if _netease is None:
        _netease = NeteaseCloud()
    return _netease


@app.get("/api/status")
def api_status():
    try:
        r = _get_redis()
        current_raw = r.get("music:current")
        play_state_raw = r.get("music:play_state")

        if not current_raw:
            return JSONResponse({"playing": False})

        current = json.loads(current_raw)
        progress = 0.0
        try:
            duration = float(current.get("duration", 0) or 0) / 1000
        except (ValueError, TypeError):
            duration = 0.0

        if play_state_raw:
            ps = json.loads(play_state_raw)
            start = float(ps.get("start_time", 0) or 0)
            dur = float(ps.get("duration", 0) or 0)
            if dur:
                duration = dur
            if start and duration:
                progress = time.time() - start

        return JSONResponse({
            "playing": True,
            "id": current.get("id"),
            "name": current.get("name", ""),
            "artists": current.get("artists", ""),
            "album": current.get("album", ""),
            "cover": current.get("cover", ""),
            "duration": duration,
            "durationText": current.get("durationText", ""),
            "progress": round(progress, 2),
        })
    except Exception as e:
        logger.error(f"/api/status 异常: {e}")
        return JSONResponse({"playing": False, "error": str(e)})


@app.get("/api/lyric")
def api_lyric(id: int = Query(...)):
    try:
        with _lyric_lock:
            if id in _lyric_cache:
                cached = _lyric_cache[id]
                return JSONResponse({"id": id, "lyric": cached})

        lyric = _get_netease().get_lyric(id)

        with _lyric_lock:
            if len(_lyric_cache) >= _LYRIC_CACHE_MAX:
                oldest = next(iter(_lyric_cache))
                del _lyric_cache[oldest]
            _lyric_cache[id] = lyric

        return JSONResponse({"id": id, "lyric": lyric})
    except Exception as e:
        logger.error(f"/api/lyric 异常: {e}")
        return JSONResponse({"id": id, "lyric": None, "error": str(e)})


@app.get("/api/queue")
def api_queue():
    try:
        r = _get_redis()
        items = r.lrange("music:queue", 0, -1)
        queue = []
        for item in items:
            song = json.loads(item)
            queue.append({
                "id": song.get("id"),
                "name": song.get("name", ""),
                "artists": song.get("artists", ""),
                "cover": song.get("cover", ""),
                "durationText": song.get("durationText", ""),
            })
        return JSONResponse({"queue": queue})
    except Exception as e:
        logger.error(f"/api/queue 异常: {e}")
        return JSONResponse({"queue": [], "error": str(e)})


@app.get("/api/debug")
def api_debug():
    """调试端点：显示 Redis 中的原始数据"""
    try:
        r = _get_redis()
        r.ping()
        current = r.get("music:current")
        play_state = r.get("music:play_state")
        queue_len = r.llen("music:queue")
        return JSONResponse({
            "redis": "connected",
            "music:current": json.loads(current) if current else None,
            "music:play_state": json.loads(play_state) if play_state else None,
            "queue_length": queue_len,
        })
    except Exception as e:
        return JSONResponse({"redis": "error", "detail": str(e)})


@app.get("/", response_class=HTMLResponse)
def index():
    html_path = os.path.join(os.path.dirname(__file__), "player.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


def run_server(host: str = "0.0.0.0", port: int = 8080):
    logger.info(f"Web 播放器启动: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
