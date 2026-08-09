import ast
import copy
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


import web.web_player_config as cfg  # noqa: E402


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

    def test_trusted_proxy_cidrs_default_to_loopback_only(self) -> None:
        marker = object()
        old = cfg.WEB_PLAYER_CONFIG.pop("trusted_proxy_cidrs", marker)
        try:
            self.assertEqual(
                cfg.trusted_proxy_cidrs(),
                ("127.0.0.1/32", "::1/128"),
            )
        finally:
            if old is not marker:
                cfg.WEB_PLAYER_CONFIG["trusted_proxy_cidrs"] = old

    def test_invalid_trusted_proxy_cidr_is_rejected(self) -> None:
        original = copy.deepcopy(cfg.WEB_PLAYER_CONFIG)
        try:
            cfg.WEB_PLAYER_CONFIG["trusted_proxy_cidrs"] = ["not-a-cidr"]
            with self.assertRaises(ValueError):
                cfg.trusted_proxy_cidrs()
        finally:
            cfg.WEB_PLAYER_CONFIG.clear()
            cfg.WEB_PLAYER_CONFIG.update(original)

    def test_hot_update_rejects_invalid_trusted_proxy_cidr(self) -> None:
        _applied, errors, _persist = cfg.apply_config_updates(
            {"web_player": {"trusted_proxy_cidrs": ["bad-network"]}}
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("无效网段", errors[0])

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

    def test_persist_updates_replace_symlink_target_without_replacing_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "runtime.py"
            link = Path(tmp) / "config.py"
            target.write_text(
                'ADMIN_UIDS = []\nWEB_PLAYER_CONFIG = {"url": ""}\n',
                encoding="utf-8",
            )
            link.symlink_to(target)

            cfg.persist_config_updates(
                {"web_player": {"url": "https://docker.example"}},
                path=str(link),
            )
            cfg.persist_admin_uids(["docker-admin"], path=str(link))

            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), target)
            self.assertFalse(Path(f"{target}.tmp").exists())
            updated = target.read_text(encoding="utf-8")
            self.assertIn("https://docker.example", updated)
            self.assertIn("docker-admin", updated)

    def test_concurrent_config_and_admin_updates_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.py"
            config_path.write_text(
                'ADMIN_UIDS = []\nWEB_PLAYER_CONFIG = {"url": ""}\n',
                encoding="utf-8",
            )
            config_path.chmod(0o640)
            barrier = threading.Barrier(2)
            errors: list[BaseException] = []
            real_lock = cfg.config_file_write_lock

            @contextmanager
            def synchronized_lock():
                barrier.wait(timeout=2)
                with real_lock():
                    yield

            def update_config() -> None:
                try:
                    cfg.persist_config_updates(
                        {"web_player": {"url": "https://concurrent.example"}},
                        path=str(config_path),
                    )
                except BaseException as exc:
                    errors.append(exc)

            def update_admins() -> None:
                try:
                    cfg.persist_admin_uids(["concurrent-admin"], path=str(config_path))
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch.object(cfg, "config_file_write_lock", synchronized_lock):
                threads = [
                    threading.Thread(target=update_config),
                    threading.Thread(target=update_admins),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=3)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            updated = config_path.read_text(encoding="utf-8")
            self.assertIn("https://concurrent.example", updated)
            self.assertIn("concurrent-admin", updated)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(list(config_path.parent.glob(".config.py.*")), [])

    def test_sensitive_transaction_artifacts_are_ignored(self) -> None:
        git_patterns = {
            "/.config.py.*.tmp",
            "/.config.py.*.bak",
            "/.private_key.py.*.tmp",
            "/.private_key.py.*.bak",
            "/config/.runtime.py.*.tmp",
            "/config/.runtime.py.*.bak",
            "/config/.private_key.py.*.tmp",
            "/config/.private_key.py.*.bak",
        }
        docker_patterns = {pattern.removeprefix("/") for pattern in git_patterns}
        gitignore = set((REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
        dockerignore = set(
            (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        )

        self.assertTrue(git_patterns <= gitignore)
        self.assertTrue(docker_patterns <= dockerignore)


if __name__ == "__main__":
    unittest.main()
