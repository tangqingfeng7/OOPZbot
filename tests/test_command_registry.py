"""命令注册表派生结果的等价性测试。

锁定「从注册表派生的权限规则」与迁移前的硬编码名单完全一致，确保 2a 阶段零行为变更。
下方两份 golden 快照是 public_command_rules.py 改为派生之前的原始值。
"""

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


# --- 迁移前的原始硬编码值（golden snapshot） ---

_LEGACY_ADMIN_ONLY_COMMANDS = frozenset(
    {
        "/plugins",
        "/loadplugin",
        "/unloadplugin",
        "/reloadplugin",
        "/禁言",
        "/mute",
        "/解禁",
        "/unmute",
        "/禁麦",
        "/mutemic",
        "/解麦",
        "/unmutemic",
        "/ban",
        "/unblock",
        "/blocklist",
        "/autorecall",
        "/recall",
        "/clear",
        "/schedule",
        "/addrole",
        "/removerole",
    }
)

_LEGACY_ADMIN_ONLY_MENTION_PREFIXES = (
    "加载插件", "启用插件", "loadplugin",
    "卸载插件", "禁用插件", "unloadplugin",
    "重载插件", "刷新插件", "reloadplugin",
    "插件列表", "扩展列表", "插件",
    "禁言", "解除禁言", "解禁",
    "禁麦", "解除禁麦", "解麦",
    "移出域", "踢出", "移出",
    "解除域内封禁", "解封",
    "封禁列表", "封禁名单", "黑名单",
    "自动撤回", "撤回",
    "清理历史", "清理记录", "清除历史", "清空历史", "清理数据",
    "定时消息列表", "定时消息",
    "添加定时消息", "新增定时消息", "删除定时消息", "移除定时消息",
    "开启定时消息", "启用定时消息", "关闭定时消息", "停用定时消息",
    "给身份组", "添加身份组", "addrole",
    "取消身份组", "移除身份组", "removerole",
)


class CommandRegistryDerivationTest(unittest.TestCase):
    def test_admin_slash_commands_match_legacy(self) -> None:
        from domain.routing.public_command_rules import ADMIN_ONLY_COMMANDS

        self.assertEqual(set(ADMIN_ONLY_COMMANDS), set(_LEGACY_ADMIN_ONLY_COMMANDS))

    def test_admin_mention_prefixes_match_legacy(self) -> None:
        from domain.routing.public_command_rules import ADMIN_ONLY_MENTION_PREFIXES

        # 消费方使用 any(startswith(...))，顺序无关，故比较集合。
        self.assertEqual(
            set(ADMIN_ONLY_MENTION_PREFIXES), set(_LEGACY_ADMIN_ONLY_MENTION_PREFIXES)
        )

    def test_no_mention_prefix_lost_to_dedup(self) -> None:
        # 注册表内 mention 别名应无重复（否则会掩盖映射错误）。
        from domain.routing.command_registry import COMMANDS

        seen: dict[str, str] = {}
        for spec in COMMANDS:
            for alias in spec.mention:
                self.assertNotIn(
                    alias, seen, f"mention 别名 {alias!r} 同时属于 {seen.get(alias)} 与 {spec.id}"
                )
                seen[alias] = spec.id

    def test_no_slash_alias_duplicated(self) -> None:
        from domain.routing.command_registry import COMMANDS

        seen: dict[str, str] = {}
        for spec in COMMANDS:
            for alias in spec.slash:
                self.assertNotIn(
                    alias, seen, f"slash 别名 {alias!r} 同时属于 {seen.get(alias)} 与 {spec.id}"
                )
                seen[alias] = spec.id


class SlashRouterAliasGoldenTest(unittest.TestCase):
    """锁定 slash 路由各规则组的别名集合（迁移前 golden），确保 派生零变更。"""

    def _router(self):
        from unittest.mock import Mock
        from app.services.routing.slash_command_router import SlashCommandRouter

        return SlashCommandRouter(Mock())

    @staticmethod
    def _aliases(rules) -> set[str]:
        collected: set[str] = set()
        for rule in rules:
            collected.update(rule[0])
        return collected

    def test_admin_rules_aliases(self) -> None:
        router = self._router()
        self.assertEqual(
            self._aliases(router._admin_rules("c", "a")),
            {"/plugins", "/loadplugin", "/unloadplugin", "/reloadplugin"},
        )

    def test_exact_rules_aliases(self) -> None:
        router = self._router()
        self.assertEqual(
            self._aliases(router._exact_rules("c", "a", "u", "")),
            {
                "/members", "/online", "/me", "/myinfo", "/voice",
                "/daily", "/quote", "/health", "/doctor", "/setup", "/wizard",
                "/禁言", "/mute", "/解禁", "/unmute", "/禁麦", "/mutemic",
                "/解麦", "/unmutemic", "/ban", "/unblock", "/blocklist",
                "/autorecall", "/recall", "/ranking", "/活跃", "/活跃排行",
                "/chatstats", "/频道统计", "/topsongs", "/点歌排行", "/播放排行",
                "/recentsongs", "/最近播放",
            },
        )

    def test_arg_rules_aliases(self) -> None:
        router = self._router()
        self.assertEqual(
            self._aliases(router._arg_rules("c", "a")),
            {"/whois", "/role", "/roles", "/search", "/help", "/enter", "/songsearch", "/pick"},
        )

    def test_pair_rules_aliases(self) -> None:
        router = self._router()
        self.assertEqual(
            self._aliases(router._pair_rules("c", "a")),
            {"/addrole", "/removerole"},
        )

    def test_inline_command_aliases(self) -> None:
        from domain.routing.command_registry import slash_of

        self.assertEqual(set(slash_of("clear_history")), {"/clear"})
        self.assertEqual(set(slash_of("remind")), {"/remind", "/提醒"})
        self.assertEqual(set(slash_of("schedule")), {"/schedule"})
        self.assertEqual(set(slash_of("clear_ai_memory")), {"/clearai", "/清除记忆", "/重置对话"})


class MentionRouterAliasGoldenTest(unittest.TestCase):
    """锁定 mention 路由各规则组的别名集合（迁移前 golden），确保 派生零变更。"""

    def _router(self):
        from unittest.mock import Mock
        from app.services.routing.mention_command_router import MentionCommandRouter

        return MentionCommandRouter(Mock())

    @staticmethod
    def _aliases(rules) -> set[str]:
        collected: set[str] = set()
        for rule in rules:
            collected.update(rule[0])
        return collected

    def test_exact_rules_aliases(self) -> None:
        router = self._router()
        self.assertEqual(
            self._aliases(router._exact_rules("c", "a", "u")),
            {
                "成员", "在线", "成员列表", "谁在线",
                "个人信息", "我是谁", "信息",
                "我的资料", "我的详细资料", "我的信息",
                "语音", "语音频道", "语音在线", "谁在语音",
                "每日一句", "一句", "名言", "语录", "鸡汤",
                "体检", "系统体检", "健康检查",
                "首启向导", "向导",
                "清理历史", "清理记录", "清除历史", "清空历史", "清理数据",
                "封禁列表", "封禁名单", "黑名单",
                "插件列表", "扩展列表", "插件",
                "帮助", "help", "指令", "命令",
                "活跃排行", "活跃榜", "排行榜",
                "频道统计", "消息统计",
                "点歌排行", "播放排行", "热歌榜",
                "最近播放", "播放历史",
                "定时消息列表", "定时消息",
                "我的提醒", "提醒列表",
                "清除记忆", "重置对话", "清除对话", "清空记忆",
            },
        )

    def test_arg_rules_aliases(self) -> None:
        router = self._router()
        self.assertEqual(
            self._aliases(router._arg_rules("c", "a", "u")),
            {
                "查看", "资料", "查询资料",
                "角色",
                "可分配角色", "分配角色",
                "搜索成员", "搜索", "找人",
                "帮助", "help",
                "进入频道", "进入",
                "加载插件", "启用插件", "loadplugin",
                "卸载插件", "禁用插件", "unloadplugin",
                "重载插件", "刷新插件", "reloadplugin",
                "画", "画一个", "画一张", "生成图片", "生成", "画图",
            },
        )

    def test_pair_rules_aliases(self) -> None:
        router = self._router()
        self.assertEqual(
            self._aliases(router._pair_rules("c", "a")),
            {"给身份组", "添加身份组", "addrole", "取消身份组", "移除身份组", "removerole"},
        )

    def test_raw_rules_aliases(self) -> None:
        router = self._router()
        self.assertEqual(
            self._aliases(router._raw_rules("c", "a", "u")),
            {
                "禁言", "解除禁言", "解禁", "禁麦", "解除禁麦", "解麦",
                "移出域", "踢出", "移出", "解除域内封禁", "解封",
                "自动撤回", "撤回", "提醒", "删除提醒", "取消提醒",
                "添加定时消息", "新增定时消息", "删除定时消息", "移除定时消息",
                "开启定时消息", "启用定时消息", "关闭定时消息", "停用定时消息",
                "选择", "选歌", "搜歌", "搜索歌曲",
            },
        )

    def test_schedule_literal_groups_stay_in_sync_with_registry(self) -> None:
        # 定时消息别名仍保留字面量；用此测试防止其与注册表静默背离。
        from domain.routing.command_registry import mention_of

        literal_schedule = {
            "定时消息列表", "定时消息",
            "添加定时消息", "新增定时消息",
            "删除定时消息", "移除定时消息",
            "开启定时消息", "启用定时消息",
            "关闭定时消息", "停用定时消息",
        }
        self.assertEqual(literal_schedule, set(mention_of("schedule")))

    def test_help_arg_subset_within_registry(self) -> None:
        from domain.routing.command_registry import mention_of

        self.assertTrue({"帮助", "help"}.issubset(set(mention_of("help"))))


class HelpCatalogDerivationTest(unittest.TestCase):
    """锁定 help_catalog 的主题级管理员分类派生，确保零行为变更且不与注册表背离。"""

    # 迁移前 ADMIN_ONLY_TOPICS 的原始硬编码值（golden snapshot）。
    _LEGACY_ADMIN_ONLY_TOPICS = frozenset({"plugin", "schedule"})

    def test_admin_only_topics_match_legacy(self) -> None:
        from app.services.interaction.help_catalog import ADMIN_ONLY_TOPICS

        self.assertEqual(set(ADMIN_ONLY_TOPICS), set(self._LEGACY_ADMIN_ONLY_TOPICS))

    def test_every_registry_topic_has_catalog_entry(self) -> None:
        # 注册表引用的每个 help_topic 都必须在 HELP_TOPICS 中存在，杜绝悬挂引用。
        from app.services.interaction.help_catalog import HELP_TOPICS
        from domain.routing.command_registry import COMMANDS

        referenced = {spec.help_topic for spec in COMMANDS if spec.help_topic is not None}
        dangling = referenced - set(HELP_TOPICS)
        self.assertEqual(dangling, set(), f"注册表引用了不存在的帮助主题: {sorted(dangling)}")

    def test_admin_only_topics_consistent_with_independent_rule(self) -> None:
        # 独立重算「主题内命令全为 admin 才算管理员主题」，防止派生逻辑回归。
        from app.services.interaction.help_catalog import ADMIN_ONLY_TOPICS
        from domain.routing.command_registry import COMMANDS

        by_topic: dict[str, bool] = {}
        for spec in COMMANDS:
            if spec.help_topic is None:
                continue
            by_topic[spec.help_topic] = by_topic.get(spec.help_topic, True) and spec.admin
        expected = {topic for topic, admin_only in by_topic.items() if admin_only}
        self.assertEqual(set(ADMIN_ONLY_TOPICS), expected)


class OverviewMenuDerivationTest(unittest.TestCase):
    """锁定 overview 总览菜单的派生，确保派生输出与迁移前逐字一致且不与主题表背离。"""

    # 迁移前 overview.lines 的原始硬编码值（golden snapshot）。
    _LEGACY_OVERVIEW_LINES = (
        "帮助主题:",
        "  帮助 音乐  点歌、队列、喜欢列表",
        "  帮助 查询  成员、资料、语音、每日一句",
        "  帮助 提醒  提醒、排行、统计",
        "  帮助 管理  禁言、撤回、清理、身份组",
        "  帮助 定时  定时消息与提醒管理",
        "  帮助 插件  插件命令与管理",
        "  帮助 AI    AI 聊天与画图",
        "  帮助 系统  系统体检与首启向导",
    )

    def test_overview_lines_match_legacy(self) -> None:
        from app.services.interaction.help_catalog import HELP_TOPICS

        self.assertEqual(HELP_TOPICS["overview"].lines, self._LEGACY_OVERVIEW_LINES)

    def test_menu_order_covers_all_non_overview_topics(self) -> None:
        # 菜单顺序正好覆盖除 overview 外的全部主题，新增/删除主题会强制更新菜单。
        from app.services.interaction.help_catalog import HELP_TOPICS, MENU_ORDER

        non_overview = set(HELP_TOPICS) - {"overview"}
        self.assertEqual(set(MENU_ORDER), non_overview)
        self.assertEqual(len(MENU_ORDER), len(non_overview), "MENU_ORDER 含重复主题")

    def test_menu_topics_carry_label_and_blurb(self) -> None:
        from app.services.interaction.help_catalog import HELP_TOPICS, MENU_ORDER

        for key in MENU_ORDER:
            topic = HELP_TOPICS[key]
            self.assertTrue(topic.menu_label, f"主题 {key} 缺少 menu_label")
            self.assertTrue(topic.menu_blurb, f"主题 {key} 缺少 menu_blurb")


class CommandSuggestionsConsistencyTest(unittest.TestCase):
    """守卫 COMMAND_SUGGESTIONS 的触发词不与命令体系背离（2d）。"""

    # 非注册表来源的合法触发词（音乐命令由音乐服务独立分发，不在注册表内）。
    _NON_REGISTRY_TRIGGERS = frozenset({"播放", "/bf"})

    def test_every_suggestion_trigger_is_known(self) -> None:
        from app.services.interaction.help_catalog import COMMAND_SUGGESTIONS
        from domain.routing.command_registry import COMMANDS

        registry_aliases = {
            alias for spec in COMMANDS for alias in (*spec.slash, *spec.mention)
        }
        known = registry_aliases | self._NON_REGISTRY_TRIGGERS
        unknown = {trigger for trigger, _ in COMMAND_SUGGESTIONS if trigger not in known}
        self.assertEqual(
            unknown, set(), f"建议触发词未对应任何命令别名(疑似改名/删除漂移): {sorted(unknown)}"
        )


if __name__ == "__main__":
    unittest.main()
