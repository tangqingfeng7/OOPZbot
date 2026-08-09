"""PluginCommandMixin 的命令入口行为测试。

锁定 mention 前缀剥离、slash 命令匹配与异常包装这套共享样板的契约，
确保迁移到 mixin 的插件（apex / steam_price 等）行为不变。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

for _path in (REPO_ROOT, SRC_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


from domain.plugins.base import BotModule, PluginCommandCapabilities, PluginMetadata  # noqa: E402
from plugins._shared.command_mixin import PluginCommandMixin  # noqa: E402


class _FakePlugin(PluginCommandMixin, BotModule):
    command_error_prefix = "测试出错"
    command_log_name = "FakePlugin"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.raise_on: str | None = None

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(name="fake", description="测试插件")

    @property
    def command_capabilities(self) -> PluginCommandCapabilities:
        return PluginCommandCapabilities(
            mention_prefixes=("steam", "Steam"),
            slash_commands=("/steam",),
        )

    def dispatch_command(self, command_text, channel, area, user, handler) -> None:
        if self.raise_on is not None and command_text == self.raise_on:
            raise RuntimeError("boom")
        self.calls.append(command_text)


class PluginCommandMixinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = _FakePlugin()
        self.handler = Mock()

    def test_mention_strips_prefix_and_dispatches(self) -> None:
        handled = self.plugin.handle_mention("steam 关注 Hades", "c", "a", "u", self.handler)
        self.assertTrue(handled)
        self.assertEqual(self.plugin.calls, ["关注 Hades"])

    def test_mention_without_prefix_not_handled(self) -> None:
        handled = self.plugin.handle_mention("apex 战绩", "c", "a", "u", self.handler)
        self.assertFalse(handled)
        self.assertEqual(self.plugin.calls, [])

    def test_slash_matches_and_joins_subcommand_arg(self) -> None:
        handled = self.plugin.handle_slash("/steam", "watch", "Hades", "c", "a", "u", self.handler)
        self.assertTrue(handled)
        self.assertEqual(self.plugin.calls, ["watch Hades"])

    def test_slash_case_insensitive(self) -> None:
        handled = self.plugin.handle_slash("/STEAM", None, None, "c", "a", "u", self.handler)
        self.assertTrue(handled)
        self.assertEqual(self.plugin.calls, [""])

    def test_slash_other_command_not_handled(self) -> None:
        handled = self.plugin.handle_slash("/apex", None, None, "c", "a", "u", self.handler)
        self.assertFalse(handled)
        self.assertEqual(self.plugin.calls, [])

    def test_dispatch_exception_replies_with_error_prefix(self) -> None:
        self.plugin.raise_on = "炸"
        handled = self.plugin.handle_mention("steam 炸", "chan", "area", "u", self.handler)
        self.assertTrue(handled)
        self.handler.sender.send_message.assert_called_once_with(
            "测试出错: boom", channel="chan", area="area"
        )


if __name__ == "__main__":
    unittest.main()
