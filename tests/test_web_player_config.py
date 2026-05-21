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
