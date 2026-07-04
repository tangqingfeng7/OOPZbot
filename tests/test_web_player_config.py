import ast
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


import web.web_player_config as cfg


class WebPlayerConfigPersistTest(unittest.TestCase):
    def _load_assignments(self, path: Path) -> dict:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        values = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = ast.literal_eval(node.value)
        return values

    def _config_example_dicts(self) -> dict:
        tree = ast.parse((REPO_ROOT / "config.example.py").read_text(encoding="utf-8"))
        result = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = ast.literal_eval(node.value)
        return result

    def test_schema_defaults_match_config_example(self) -> None:
        """CONFIG_FIELD_SCHEMA 的默认值是单一来源，必须与 config.example.py 完全一致。"""
        example = self._config_example_dicts()
        for group, fields in cfg.GROUP_DEFAULTS.items():
            source_name = cfg.CONFIG_GROUP_SOURCES[group]
            self.assertIn(source_name, example, f"config.example.py 缺少 {source_name}")
            for field, default in fields.items():
                self.assertEqual(
                    example[source_name].get(field),
                    default,
                    f"{group}.{field} 默认值与 config.example.py 漂移",
                )

    def test_config_field_schema_omits_sensitive_defaults(self) -> None:
        """敏感字段不下发明文默认；普通字段必须带 type 与 default。"""
        schema = cfg.config_field_schema()
        self.assertNotIn("default", schema["web_player"]["admin_password"])
        self.assertEqual(schema["web_player"]["port"]["default"], 8080)
        self.assertEqual(schema["web_player"]["host"]["default"], "0.0.0.0")
        self.assertTrue(schema["web_player"]["send_link_enabled"]["default"])
        self.assertEqual(schema["music"]["default_volume"]["default"], 50)
        self.assertEqual(schema["web_player"]["port"]["type"], "int")

    def test_web_host_port_fall_back_to_schema_defaults(self) -> None:
        self.assertEqual(cfg.config_default("web_player", "port"), 8080)
        self.assertEqual(cfg.config_default("web_player", "host"), "0.0.0.0")
        self.assertIsInstance(cfg.web_port(), int)
        self.assertTrue(cfg.web_host())

    def test_config_snapshot_uses_schema_default_for_missing_field(self) -> None:
        marker = object()
        old_value = cfg.WEB_PLAYER_CONFIG.pop("send_link_enabled", marker)
        try:
            snapshot = cfg.config_snapshot()
        finally:
            if old_value is not marker:
                cfg.WEB_PLAYER_CONFIG["send_link_enabled"] = old_value

        self.assertTrue(snapshot["web_player"]["send_link_enabled"])

    def test_persist_config_updates_creates_missing_music_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.py"
            config_path.write_text(
                'WEB_PLAYER_CONFIG = {\n'
                '    "url": "",\n'
                '}\n',
                encoding="utf-8",
            )

            cfg.persist_config_updates(
                {"music": {"default_volume": 50}},
                path=str(config_path),
            )

            assignments = self._load_assignments(config_path)
            self.assertIn("MUSIC_CONFIG", assignments)
            self.assertEqual(assignments["MUSIC_CONFIG"]["default_volume"], 50)
            self.assertIn("auto_play_enabled", assignments["MUSIC_CONFIG"])


if __name__ == "__main__":
    unittest.main()
