import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


class DeadCodeCleanupContractsTest(unittest.TestCase):
    def test_removed_legacy_files_and_dependency_stay_removed(self) -> None:
        removed_paths = (
            "src/web/assets/admin/webintosh-admin.css",
            "plugins/_shared/lol_common.py",
            "tools/audio_service.py",
        )
        for relative_path in removed_paths:
            with self.subTest(path=relative_path):
                self.assertFalse((REPO_ROOT / relative_path).exists())

        self.assertNotIn("psutil", _read("requirements.txt"))
        self.assertNotIn("audio_service.py", _read("docs/architecture.md"))

    def test_web_player_only_mounts_the_current_static_assets(self) -> None:
        source = _read("src/web/web_player.py")

        self.assertIn('"/admin-assets"', source)
        self.assertNotIn("webintosh-assets", source)
        self.assertNotIn("Webintosh", source)

    def test_agora_page_only_exports_browser_methods_used_by_python(self) -> None:
        page = _read("src/web/assets/agora_player.html")
        removed_methods = (
            "agoraPlayLocal",
            "agoraGetCurrentTime",
            "agoraSendIdentity",
            "agoraVoiceDebug",
        )
        for method in removed_methods:
            with self.subTest(method=method):
                self.assertNotIn(method, page)

        for method in ("agoraPlayAudio", "agoraSetVoiceIdentity", "agoraState"):
            with self.subTest(required_method=method):
                self.assertIn(method, page)

    def test_plugin_runtime_accessor_comes_from_shared_runtime_module(self) -> None:
        source = _read("src/web/admin/plugins.py")

        self.assertIn("from web.admin.shared import _get_plugin_runtime, cfg", source)
        self.assertNotIn("def _get_plugin_runtime", source)
        self.assertIn("def _get_plugin_host", source)

    def test_music_keeps_new_bypass_construction_guards(self) -> None:
        source = _read("src/music/music.py")

        self.assertGreaterEqual(
            source.count('hasattr(self, "_cover_prefetch_lock")'),
            2,
        )


if __name__ == "__main__":
    unittest.main()
