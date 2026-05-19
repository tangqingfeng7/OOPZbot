import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


from app.infrastructure.plugin_runtime import config_assets, loader, module_tools
from domain.plugins.plugin_name import normalize_plugin_name
from domain.plugins.base import BotModule, PluginConfigField, PluginConfigSpec, PluginMetadata


class _DemoPlugin(BotModule):
    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(name="demo_plugin", description="demo")

    @property
    def config_spec(self) -> PluginConfigSpec:
        return PluginConfigSpec((PluginConfigField("enabled", default=False),))


class PluginConfigLayoutTest(unittest.TestCase):
    def test_load_plugin_config_reads_nested_config_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested_path = root / "config" / "plugins" / "demo_plugin" / "config.json"
            nested_path.parent.mkdir(parents=True)
            nested_path.write_text(json.dumps({"enabled": True}), encoding="utf-8")

            with patch.object(loader, "_PROJECT_ROOT", str(root)):
                config = loader.load_plugin_config("demo_plugin")

        self.assertTrue(config.exists)
        self.assertEqual(config["enabled"], True)
        self.assertEqual(Path(config.path), nested_path)

    def test_load_plugin_config_ignores_flat_json_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "plugins"
            flat_path = config_dir / "demo_plugin.json"
            config_dir.mkdir(parents=True)
            flat_path.write_text(json.dumps({"enabled": True}), encoding="utf-8")

            with patch.object(loader, "_PROJECT_ROOT", str(root)):
                config = loader.load_plugin_config("demo_plugin")

        self.assertFalse(config.exists)
        self.assertEqual(Path(config.path), config_dir / "demo_plugin" / "config.json")

    def test_write_plugin_config_assets_uses_plugin_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            example_path, schema_path = config_assets.write_plugin_config_assets(
                _DemoPlugin(),
                Path(tmp) / "config" / "plugins",
            )

            self.assertEqual(example_path.name, "example.json")
            self.assertEqual(schema_path.name, "schema.json")
            self.assertEqual(example_path.parent.name, "demo_plugin")
            self.assertTrue(example_path.exists())
            self.assertTrue(schema_path.exists())

    def test_plugin_discovery_accepts_package_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugins_dir = root / "plugins"
            alpha_dir = plugins_dir / "alpha"
            shared_dir = plugins_dir / "_shared"
            alpha_dir.mkdir(parents=True)
            shared_dir.mkdir()
            (alpha_dir / "__init__.py").write_text(
                "from domain.plugins.base import BotModule, PluginMetadata\n"
                "class AlphaPlugin(BotModule):\n"
                "    @property\n"
                "    def metadata(self):\n"
                "        return PluginMetadata(name='alpha')\n",
                encoding="utf-8",
            )
            (shared_dir / "__init__.py").write_text("", encoding="utf-8")
            (plugins_dir / "beta.py").write_text("", encoding="utf-8")

            names = module_tools.discover_plugin_names(project_root=str(root))
            module, module_name = module_tools.load_plugin_module("alpha", project_root=str(root))

        self.assertEqual(names, ["alpha", "beta"])
        self.assertEqual(module_name, "plugins.alpha")
        self.assertIsNotNone(module_tools.find_plugin_class(module))

    def test_plugin_name_does_not_accept_py_suffix(self) -> None:
        self.assertIsNone(normalize_plugin_name("alpha.py"))
        self.assertEqual(normalize_plugin_name("alpha"), "alpha")


if __name__ == "__main__":
    unittest.main()
