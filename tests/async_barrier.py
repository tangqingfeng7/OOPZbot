"""并发用例共用的异步
"""

from __future__ import annotations

import asyncio


class AsyncBarrier:
    def __init__(self, parties: int) -> None:
        if parties < 1:
            raise ValueError("parties 必须 >= 1")
        self._parties = parties
        self._count = 0
        self._released = asyncio.Event()

    @property
    def parties(self) -> int:
        return self._parties

    async def wait(self) -> None:
        self._count += 1
        if self._count >= self._parties:
            self._released.set()
            return
        await self._released.wait()
