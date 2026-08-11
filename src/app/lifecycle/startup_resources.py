"""启动期数据库准备；网络资源统一由 ``AppContextBuilder`` 创建。"""

from __future__ import annotations

from dataclasses import dataclass

from core.database import init_database


@dataclass(frozen=True, slots=True)
class StartupResources:
    database_ready: bool = True


class StartupResourceBuilder:
    async def build(self) -> StartupResources:
        await init_database()
        return StartupResources()
