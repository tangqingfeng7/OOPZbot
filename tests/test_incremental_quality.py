
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
QUALITY_SCRIPT = REPO_ROOT / "tools" / "check_incremental_quality.py"
QUALITY_SPEC = importlib.util.spec_from_file_location(
    "oopz_incremental_quality",
    QUALITY_SCRIPT,
)
if QUALITY_SPEC is None or QUALITY_SPEC.loader is None:
    raise RuntimeError(f"无法加载增量质量脚本: {QUALITY_SCRIPT}")
quality = importlib.util.module_from_spec(QUALITY_SPEC)
sys.modules[QUALITY_SPEC.name] = quality
QUALITY_SPEC.loader.exec_module(quality)


class IncrementalQualityDiffTest(unittest.TestCase):
    def test_pure_deletion_checks_diagnostics_on_surviving_lines(self) -> None:
        deletion_diff = "@@ -1 +0,0 @@\n-import missing_dependency\n"
        self.assertIsNone(quality._parse_hunk_lines(deletion_diff))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.py"
            source.write_text("\n" * 9 + "missing_dependency.call()\n", encoding="utf-8")
            changed = {}
            diagnostic = {
                "filename": str(source),
                "location": {"row": 10},
                "end_location": {"row": 10},
                "code": "F821",
                "message": "Undefined name `missing_dependency`",
            }

            with (
                mock.patch.object(quality, "ROOT", root),
                mock.patch.object(
                    quality,
                    "_name_status",
                    return_value=[("M", "sample.py", "sample.py")],
                ),
                mock.patch.object(
                    quality,
                    "_git",
                    side_effect=lambda *args, **_kwargs: (
                        "pass\n" if args and args[0] == "show" else deletion_diff
                    ),
                ),
            ):
                quality._merge_diff(changed, ["diff"], "HEAD")
                with (
                    mock.patch.object(quality, "_tool", return_value="ruff"),
                    mock.patch.object(
                        quality,
                        "_run_json",
                        side_effect=[[diagnostic], []],
                    ),
                ):
                    failures = quality.run_ruff(changed)

        self.assertEqual(
            failures,
            ["sample.py:10: F821 Undefined name `missing_dependency`"],
        )

    def test_existing_ruff_diagnostics_are_subtracted_by_signature_and_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.py"
            source.write_text("value = 1\n", encoding="utf-8")
            entry = quality.ChangedFile(
                source,
                None,
                ruff_baseline=quality.RuffBaseline("BASE", "sample.py"),
                subtract_ruff_baseline=True,
            )
            old_at_new_position = {
                "filename": str(source),
                "location": {"row": 2},
                "end_location": {"row": 2},
                "code": "F401",
                "message": "`os` imported but unused",
            }
            duplicate = {
                **old_at_new_position,
                "location": {"row": 8},
                "end_location": {"row": 8},
            }
            new_diagnostic = {
                "filename": str(source),
                "location": {"row": 10},
                "end_location": {"row": 10},
                "code": "F821",
                "message": "Undefined name `missing_dependency`",
            }
            baseline_diagnostic = {
                **old_at_new_position,
                "location": {"row": 100},
                "end_location": {"row": 100},
            }

            with (
                mock.patch.object(quality, "ROOT", root),
                mock.patch.object(quality, "_tool", return_value="ruff"),
                mock.patch.object(quality, "_git", return_value="import os\n"),
                mock.patch.object(
                    quality,
                    "_run_json",
                    side_effect=[
                        [old_at_new_position, duplicate, new_diagnostic],
                        [baseline_diagnostic],
                    ],
                ),
            ):
                failures = quality.run_ruff({source: entry})

        self.assertEqual(
            failures,
            [
                "sample.py:8: F401 `os` imported but unused",
                "sample.py:10: F821 Undefined name `missing_dependency`",
            ],
        )

    def test_added_file_does_not_subtract_a_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "new_file.py"
            source.write_text("missing_name\n", encoding="utf-8")
            entry = quality.ChangedFile(source, set())
            entry.check_whole_file(subtract_baseline=False)
            diagnostic = {
                "filename": str(source),
                "location": {"row": 1},
                "end_location": {"row": 1},
                "code": "F821",
                "message": "Undefined name `missing_name`",
            }

            with (
                mock.patch.object(quality, "ROOT", root),
                mock.patch.object(quality, "_tool", return_value="ruff"),
                mock.patch.object(quality, "_run_json", return_value=[diagnostic]),
            ):
                failures = quality.run_ruff({source: entry})

        self.assertEqual(failures, ["new_file.py:1: F821 Undefined name `missing_name`"])

    def test_resolve_baseline_uses_head_or_merge_base(self) -> None:
        with mock.patch.object(quality, "_git", return_value="head-sha\n") as git:
            self.assertEqual(quality._resolve_baseline(None), "head-sha")
            git.assert_called_once_with("rev-parse", "--verify", "HEAD", check=False)

        with mock.patch.object(quality, "_git", return_value="merge-sha\n") as git:
            self.assertEqual(quality._resolve_baseline("origin/main"), "merge-sha")
            git.assert_called_once_with("merge-base", "origin/main", "HEAD")


if __name__ == "__main__":
    unittest.main()
