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
from plugin_base import BotModule, PluginConfigField, PluginConfigSpec, PluginMetadata


class _DemoPlugin(BotModule):
    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(name="demo_plugin", description="demo")

    @property
    def config_spec(self) -> PluginConfigSpec:
        return PluginConfigSpec((PluginConfigField("enabled", default=False),))


class PluginConfigLayoutTest(unittest.TestCase):
    def test_load_plugin_config_prefers_nested_config_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "plugins"
            legacy_path = config_dir / "demo_plugin.json"
            nested_path = config_dir / "demo_plugin" / "config.json"
            nested_path.parent.mkdir(parents=True)
            legacy_path.write_text(json.dumps({"enabled": False}), encoding="utf-8")
            nested_path.write_text(json.dumps({"enabled": True}), encoding="utf-8")

            with patch.object(loader, "_PROJECT_ROOT", str(root)):
                config = loader.load_plugin_config("demo_plugin")

        self.assertTrue(config.exists)
        self.assertEqual(config["enabled"], True)
        self.assertEqual(Path(config.path), nested_path)

    def test_load_plugin_config_falls_back_to_legacy_flat_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "plugins"
            legacy_path = config_dir / "demo_plugin.json"
            config_dir.mkdir(parents=True)
            legacy_path.write_text(json.dumps({"enabled": True}), encoding="utf-8")

            with patch.object(loader, "_PROJECT_ROOT", str(root)):
                config = loader.load_plugin_config("demo_plugin")

        self.assertTrue(config.exists)
        self.assertEqual(config["enabled"], True)
        self.assertEqual(Path(config.path), legacy_path)

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
                "from plugin_base import BotModule, PluginMetadata\n"
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


if __name__ == "__main__":
    unittest.main()
