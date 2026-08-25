"""与应用事件循环共存的网易云 API 子进程运行时。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import aiohttp

from config import NETEASE_CLOUD
from core.http_constants import HTTP_TIMEOUT_HEALTH
from core.logger_config import setup_logger

logger = setup_logger("NeteaseApiRuntime")


class NeteaseApiRuntime:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._stop_event = asyncio.Event()

    @staticmethod
    def _project_root() -> Path:
        from core.paths import PROJECT_ROOT_PATH

        return PROJECT_ROOT_PATH

    @classmethod
    def _resolve_api_dir(cls, raw_path: str) -> Path:
        return cls._project_root() / raw_path.strip()

    async def start(self) -> None:
        self._stop_event.clear()
        path = str(NETEASE_CLOUD.get("auto_start_path", "") or "")
        if not path.strip():
            return

        if await self._api_is_ready():
            logger.info("网易云 API 已在运行，跳过重复启动。")
            return

        api_dir = self._resolve_api_dir(path)
        if not (api_dir / "app.js").is_file():
            logger.info("网易云 API 目录不存在，跳过启动: %s", api_dir)
            return

        env = os.environ.copy()
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            env.pop(key, None)
        local_bin = os.path.expanduser("~/.local/bin")
        if local_bin and local_bin not in env.get("PATH", ""):
            env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")

        logger.info("正在启动网易云 API: %s", api_dir)
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._find_node_binary(),
                "app.js",
                cwd=str(api_dir),
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if sys.platform == "win32"
                    else 0
                ),
            )
        except Exception as exc:
            logger.warning("启动网易云 API 失败: %s", exc)
            self._process = None
            return

        await self._wait_until_ready()

    @staticmethod
    async def _api_is_ready() -> bool:
        """容器或外部 API 已就绪时，不再启动本地源码进程争抢端口。"""
        base_url = str(
            NETEASE_CLOUD.get("base_url", "http://localhost:3000")
        ).rstrip("/")
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_HEALTH)
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.get(f"{base_url}/") as response:
                    return response.status < 500
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    async def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass
        except Exception as exc:
            logger.warning("停止网易云 API 时出现异常: %s", exc)
        finally:
            logger.info("网易云 API 已停止。")

    @staticmethod
    def _find_node_binary() -> str:
        node_cmd = shutil.which("node")
        if node_cmd:
            return node_cmd
        for candidate in (os.path.expanduser("~/.local/bin/node"), "/usr/bin/node"):
            if candidate and os.path.isfile(candidate):
                return candidate
        return "node"

    async def _wait_until_ready(self) -> None:
        base_url = str(
            NETEASE_CLOUD.get("base_url", "http://localhost:3000")
        ).rstrip("/")
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_HEALTH)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            for _ in range(30):
                if self._stop_event.is_set():
                    return
                process = self._process
                if process is not None and process.returncode is not None:
                    logger.warning(
                        "网易云 API 子进程已退出 (code=%s)，放弃等待。",
                        process.returncode,
                    )
                    return
                try:
                    async with session.get(f"{base_url}/") as response:
                        if response.status < 500:
                            logger.info("网易云 API 已就绪。")
                            return
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=0.5)
        logger.warning("网易云 API 启动超时，音乐功能可能不可用。")
