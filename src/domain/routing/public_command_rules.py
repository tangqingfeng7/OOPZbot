"""
命令权限规则。

两套「仅管理员」名单均从 :mod:`domain.routing.command_registry` 派生，注册表是唯一来源。
"""

from __future__ import annotations

from domain.routing.command_registry import (
    admin_mention_prefixes,
    admin_slash_commands,
)

ADMIN_ONLY_COMMANDS = admin_slash_commands()

ADMIN_ONLY_MENTION_PREFIXES = admin_mention_prefixes()


def is_public_mention_text(text: str) -> bool:
    return not any(text.startswith(prefix) for prefix in ADMIN_ONLY_MENTION_PREFIXES)


def is_public_slash_command(command: str) -> bool:
    return command.lower() not in ADMIN_ONLY_COMMANDS
