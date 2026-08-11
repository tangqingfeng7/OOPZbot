from app.services.runtime import CommandRuntimeView
from config import ADMIN_UIDS
from domain.routing.public_command_rules import (
    is_public_mention_text,
    is_public_slash_command,
)


class CommandAccessService:
    def __init__(self, runtime: CommandRuntimeView):
        self._bot_mention = runtime.bot_mention
        self._plugins = runtime.plugins

    @staticmethod
    def has_configured_admins() -> bool:
        """是否配置过管理员名单。"""
        return bool(ADMIN_UIDS)

    @staticmethod
    def is_admin(user: str) -> bool:
        """是否为管理员。

        fail-closed：空名单不再视作「所有人都是管理员」—— 那是首启默认态，
        等于装好即全员可用禁言/封禁/撤回/插件管理。空名单时只有公开命令可用，
        首启引导（``/setup``）会回显调用者 UID 并给出配置步骤。
        """
        return user in ADMIN_UIDS

    def is_public_command(self, content: str) -> bool:
        """命令是否对非管理员开放。

        内置名单只覆盖内置命令，插件命令不在其中 —— 所以「不在内置管理名单里」
        并不等于公开：插件自己声明的 ``is_public_command=False`` 必须先排除掉，
        否则该声明形同虚设（闸门会因为内置名单没命中而直接放行）。
        """
        if self._bot_mention and self._bot_mention in content:
            text = content.replace(self._bot_mention, "").strip()
            if self._plugins.has_admin_only_mention_prefix(text):
                return False
            if is_public_mention_text(text):
                return True
            return self._plugins.has_public_mention_prefix(text)

        if content.startswith("/"):
            command = content.split()[0].lower()
            if self._plugins.has_admin_only_slash_command(command):
                return False
            if is_public_slash_command(command):
                return True
            return self._plugins.has_public_slash_command(command)

        return False
