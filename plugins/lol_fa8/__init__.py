import contextlib

from core.constants import Msg
from domain.plugins.base import (
    BotModule,
    PluginCommandCapabilities,
    PluginConfigField,
    PluginConfigSpec,
    PluginMetadata,
)
from plugins._shared.command_mixin import PluginCommandMixin

_HELP_TEXT = (
    "请输入召唤师名称\n"
    "格式: @bot 战绩 召唤师名#编号  |  /zj 召唤师名#编号\n"
    "示例: @bot 战绩 艺术就是充钱丶#72269\n"
    "指定大区: @bot 战绩 班德尔城 召唤师名#编号  |  /zj 班德尔城 召唤师名#编号\n"
    "按区搜索: @bot 战绩 3 召唤师名#编号 (1-5对应联盟一~五区)"
)


class LolFa8Plugin(PluginCommandMixin, BotModule):
    command_error_prefix = "战绩查询出错"
    command_log_name = "LolFa8Plugin"

    def __init__(self):
        self._handler = None
        self._config: dict = {}

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="lol_fa8",
            description="LOL 战绩查询（FA8 召唤师）",
            version="1.0.0",
            author="",
        )

    @property
    def command_capabilities(self) -> PluginCommandCapabilities:
        return PluginCommandCapabilities(
            mention_prefixes=("查询战绩", "查战绩", "战绩"),
            slash_commands=("/zj",),
            is_public_command=True,
        )

    @property
    def private_modules(self) -> tuple[str, ...]:
        return ("plugins.lol_fa8.service",)

    @property
    def config_spec(self) -> PluginConfigSpec:
        return PluginConfigSpec(
            (
                PluginConfigField("enabled", default=False),
                PluginConfigField("username", default=""),
                PluginConfigField("password", default=""),
                PluginConfigField("default_area", default="1"),
            )
        )

    async def on_load(self, handler, config=None):
        self._config = (config or {}).copy()
        self._handler = None

    async def on_unload(self) -> None:
        if self._handler is not None:
            with contextlib.suppress(Exception):
                await self._handler.close()
        self._handler = None

    def _service(self):
        if self._handler is None:
            from .service import FA8Handler
            self._handler = FA8Handler(self._config)
        return self._handler

    async def dispatch_command(self, command_text, channel, area, user, handler) -> None:
        keyword = command_text.strip()
        if not keyword:
            await self._send(handler, _HELP_TEXT, channel, area)
            return
        await self._send(handler, f"{Msg.SEARCH} 正在查询 {keyword} 的战绩...", channel, area)
        reply = await self._service().query_and_format(keyword)
        await self._send(handler, reply, channel, area)
