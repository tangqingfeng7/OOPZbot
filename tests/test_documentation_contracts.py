import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


class DocumentationContractsTest(unittest.TestCase):
    def test_architecture_lists_every_core_database_table(self) -> None:
        database_source = _read("src/core/database.py")
        architecture = _read("docs/architecture.md")
        tables = set(
            re.findall(
                r"CREATE TABLE IF NOT EXISTS\s+([a-z0-9_]+)",
                database_source,
            )
        )

        self.assertEqual(len(tables), 11)
        missing = sorted(table for table in tables if f"`{table}`" not in architecture)
        self.assertEqual(missing, [])

    def test_architecture_documents_current_module_boundaries(self) -> None:
        architecture = _read("docs/architecture.md")
        required_paths = (
            "message_dispatcher.py",
            "browser_launch.py",
            "music_platform.py",
            "area_events.py",
            "src/web/admin/",
        )
        for path in required_paths:
            with self.subTest(path=path):
                self.assertIn(path, architecture)

        self.assertNotIn("Admin 后台所有路由（`APIRouter`）", architecture)
        self.assertIn("9 行稳定 facade", architecture)

    def test_architecture_documents_area_scoped_and_global_redis_keys(self) -> None:
        architecture = _read("docs/architecture.md")
        documented_keys = (
            "music:<area>:queue",
            "music:<area>:current",
            "music:<area>:default_channel",
            "music:<area>:play_state",
            "music:<area>:play_mode",
            "music:volume",
            "music:web_commands",
            "music:web_access_token",
            "music:web_active_area",
            "music:web_last_access",
            "music:admin_session:<token>",
        )
        for key in documented_keys:
            with self.subTest(key=key):
                self.assertIn(key, architecture)

    def test_api_and_player_entry_documentation_matches_routes(self) -> None:
        api_reference = _read("docs/api-reference.md")
        commands = _read("docs/commands.md")

        self.assertIn("/client/v1/area/v1/operateLogs", api_reference)
        self.assertIn("/w/{token}", commands)
        self.assertIn("GET /", commands)
        self.assertIn("HTTP 403", commands)
        self.assertNotIn(
            "访问 `http://<服务器IP>:8080/` 即可打开 Web 播放器",
            commands,
        )

    def test_scaffold_and_environment_documentation_matches_code(self) -> None:
        plugin_development = _read("docs/plugin-development.md")
        configuration = _read("docs/configuration.md")

        self.assertIn("PluginCommandMixin", plugin_development)
        self.assertIn("dispatch_command", plugin_development)
        self.assertNotIn("空实现的 `handle_mention / handle_slash`", plugin_development)

        environment_variables = (
            "OOPZ_DEBUG_WS_EVENTS",
            "BOT_CHROMIUM_EXECUTABLE_PATH",
            "CHROME_BIN",
            "CHROME_PATH",
            "OOPZ_PHONE",
            "OOPZ_PASSWORD",
        )
        for name in environment_variables:
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", configuration)

    def test_member_event_comment_and_contributor_links_stay_current(self) -> None:
        area_events = _read("src/oopz/area_events.py")
        readme = _read("README.md")

        self.assertNotIn("Leaves are not covered by polling", area_events)
        for target in (
            "plugins/README.md",
            "config/plugins/README.md",
            "src/README.md",
        ):
            with self.subTest(target=target):
                self.assertIn(f"]({target})", readme)


if __name__ == "__main__":
    unittest.main()
