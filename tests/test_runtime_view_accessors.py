"""运行时视图访问器的回退语义。

`sender_of` 这组函数要「先看视图本身，没有再退到 infrastructure」，以兼容测试桩
和精简运行时对象。注意不能写成 `getattr(view, name, view.infrastructure.x)`：
默认值是**急求值**的，只有 name 而没有 infrastructure 的对象会先炸在默认值上，
效果与意图正好相反。
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from app.services.runtime.command_runtime import (  # noqa: E402
    music_of,
    plugins_of,
    sender_of,
)

_ACCESSORS = (
    ("sender", sender_of),
    ("music", music_of),
    ("plugins", plugins_of),
)


class RuntimeViewAccessorTest(unittest.TestCase):
    def test_slim_view_without_infrastructure_is_supported(self) -> None:
        """只带目标属性的精简对象必须可用——这正是这组函数存在的理由。"""
        for name, accessor in _ACCESSORS:
            with self.subTest(attr=name):
                slim = SimpleNamespace(**{name: f"slim-{name}"})
                self.assertEqual(accessor(slim), f"slim-{name}")

    def test_full_runtime_falls_back_to_infrastructure(self) -> None:
        for name, accessor in _ACCESSORS:
            with self.subTest(attr=name):
                full = SimpleNamespace(
                    infrastructure=SimpleNamespace(**{name: f"infra-{name}"})
                )
                self.assertEqual(accessor(full), f"infra-{name}")

    def test_view_attribute_wins_over_infrastructure(self) -> None:
        for name, accessor in _ACCESSORS:
            with self.subTest(attr=name):
                both = SimpleNamespace(
                    infrastructure=SimpleNamespace(**{name: f"infra-{name}"}),
                    **{name: f"view-{name}"},
                )
                self.assertEqual(accessor(both), f"view-{name}")

    def test_infrastructure_is_not_touched_when_view_has_the_attribute(self) -> None:
        """回退必须是惰性的：视图自带属性时不得去读 infrastructure。

        读它不只是浪费——精简对象上根本没有这个属性，急求值会直接抛错。
        """
        touched: list[str] = []

        class _Tattletale:
            @property
            def infrastructure(self):
                touched.append("infrastructure")
                raise AssertionError("视图自带属性时不应触碰 infrastructure")

        for name, accessor in _ACCESSORS:
            with self.subTest(attr=name):
                view = _Tattletale()
                setattr(view, name, f"view-{name}")
                self.assertEqual(accessor(view), f"view-{name}")

        self.assertEqual(touched, [])

    def test_missing_everywhere_still_reports_the_real_gap(self) -> None:
        """两边都没有时要如实报错，而不是悄悄返回 None。"""
        for name, accessor in _ACCESSORS:
            with self.subTest(attr=name), self.assertRaises(AttributeError):
                accessor(SimpleNamespace(infrastructure=SimpleNamespace()))


if __name__ == "__main__":
    unittest.main()
