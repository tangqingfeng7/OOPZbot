
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _load_deploy() -> Any:
    """deploy.py 在仓库根目录、不是包的一部分，按路径加载。

    标成 Any：类型检查器推不出动态加载模块的属性，而测试要 patch 其中的常量。
    """
    spec = importlib.util.spec_from_file_location("deploy_script", REPO_ROOT / "deploy.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy: Any = _load_deploy()


class CredentialDetectionTest(unittest.TestCase):
    """凭据判断错了会导致「没配也去启动」或「配了却不启动」。"""

    def test_phone_and_password_is_enough(self) -> None:
        self.assertTrue(
            deploy.has_usable_credentials(
                {"login_phone": "138", "login_password": "pw", "device_id": "",
                 "person_uid": "", "jwt_token": ""}
            )
        )

    def test_full_token_triple_is_enough(self) -> None:
        self.assertTrue(
            deploy.has_usable_credentials(
                {"login_phone": "", "login_password": "", "device_id": "d",
                 "person_uid": "p", "jwt_token": "t"}
            )
        )

    def test_partial_token_triple_is_not_enough(self) -> None:
        self.assertFalse(
            deploy.has_usable_credentials(
                {"login_phone": "", "login_password": "", "device_id": "d",
                 "person_uid": "p", "jwt_token": ""}
            ),
            "缺一项就连不上，不能当作已配置",
        )

    def test_phone_without_password_is_not_enough(self) -> None:
        self.assertFalse(
            deploy.has_usable_credentials(
                {"login_phone": "138", "login_password": "", "device_id": "",
                 "person_uid": "", "jwt_token": ""}
            )
        )

    def test_empty_config_is_not_enough(self) -> None:
        self.assertFalse(
            deploy.has_usable_credentials(
                dict.fromkeys(
                    ["login_phone", "login_password", "device_id", "person_uid", "jwt_token"], ""
                )
            )
        )


class NeteaseDirTest(unittest.TestCase):
    """回归背景：目录名写死成 NeteaseCloudMusicApi 会把用户改过名的
    （比如 NeteaseAPI_tmp）已装环境误报成「没装」。
    """

    def test_follows_configured_path(self) -> None:
        with patch.object(deploy, "config_value", return_value="NeteaseAPI_tmp"):
            self.assertEqual(deploy.netease_dir(), deploy.ROOT / "NeteaseAPI_tmp")

    def test_falls_back_to_default_when_unset(self) -> None:
        with patch.object(deploy, "config_value", return_value=""):
            self.assertEqual(
                deploy.netease_dir(), deploy.ROOT / deploy.NETEASE_DEFAULT_DIR
            )

    def test_absolute_path_is_kept(self) -> None:
        with patch.object(deploy, "config_value", return_value="/opt/ncm"):
            self.assertEqual(deploy.netease_dir(), Path("/opt/ncm"))


class ConfigWriteTest(unittest.TestCase):
    """写凭据不能破坏 config.py：它是会被 import 的 Python 源码。"""

    def test_writes_values_without_breaking_the_file(self) -> None:
        from oopz.credentials import _replace_config_value

        original = (
            "OOPZ_CONFIG = {\n"
            '    "login_phone": "",     # 账号\n'
            '    "login_password": "",  # 密码\n'
            '    "base_url": "https://gateway.oopz.cn",\n'
            "}\n"
        )
        content, ok_phone = _replace_config_value(original, "login_phone", "13800000000")
        content, ok_pw = _replace_config_value(content, "login_password", "secret")

        self.assertTrue(ok_phone and ok_pw)
        self.assertIn('"login_phone": "13800000000"', content)
        self.assertIn('"login_password": "secret"', content)
        self.assertIn("# 账号", content, "注释要保留，用户还得看着它改配置")
        self.assertIn('"base_url": "https://gateway.oopz.cn"', content, "别的字段不能动")

        namespace: dict = {}
        exec(compile(content, "config.py", "exec"), namespace)
        self.assertEqual(namespace["OOPZ_CONFIG"]["login_phone"], "13800000000")


class SectionAwareConfigWriteTest(unittest.TestCase):
    """api_key / cookie 这类键名多个配置块里都有，写错块是静默的。"""

    SAMPLE = (
        'OOPZ_CONFIG = {\n    "proxy": "",\n}\n\n'
        'NETEASE_CLOUD = {\n    "cookie": "",   # 网易云\n}\n\n'
        'DOUBAO_CONFIG = {\n    "enabled": False,\n    "api_key": "",\n}\n\n'
        'DOUBAO_IMAGE_CONFIG = {\n    "enabled": False,\n    "api_key": "",\n}\n\n'
        'ADMIN_UIDS = [\n    # "用户UID",\n]\n'
    )

    def test_writes_into_the_right_section(self) -> None:
        content, ok = deploy.set_config_field(self.SAMPLE, "DOUBAO_CONFIG", "api_key", "ark-key")

        self.assertTrue(ok)
        self.assertIn('DOUBAO_CONFIG = {\n    "enabled": False,\n    "api_key": "ark-key",', content)
        self.assertIn('DOUBAO_IMAGE_CONFIG = {\n    "enabled": False,\n    "api_key": "",', content)

    def test_cookie_goes_to_netease_not_elsewhere(self) -> None:
        content, ok = deploy.set_config_field(self.SAMPLE, "NETEASE_CLOUD", "cookie", "MUSIC_U=x")

        self.assertTrue(ok)
        self.assertIn('"cookie": "MUSIC_U=x",   # 网易云', content, "注释要留着")

    def test_boolean_field_is_written_without_quotes(self) -> None:
        content, ok = deploy.set_config_field(self.SAMPLE, "DOUBAO_CONFIG", "enabled", True)

        self.assertTrue(ok)
        self.assertIn('"enabled": True', content)
        self.assertNotIn('"enabled": "True"', content, "布尔值加引号会变成真值字符串")

    def test_unknown_section_is_a_no_op(self) -> None:
        content, ok = deploy.set_config_field(self.SAMPLE, "NO_SUCH_CONFIG", "api_key", "x")

        self.assertFalse(ok)
        self.assertEqual(content, self.SAMPLE, "找不到配置块时不能乱改")

    def test_admin_uids_list_is_rewritten(self) -> None:
        content, ok = deploy.set_admin_uids(self.SAMPLE, ["uid-1", "uid-2"])

        self.assertTrue(ok)
        namespace: dict = {}
        exec(compile(content, "config.py", "exec"), namespace)
        self.assertEqual(namespace["ADMIN_UIDS"], ["uid-1", "uid-2"])

    def test_result_is_still_valid_python(self) -> None:
        content = self.SAMPLE
        for section, key, value in (
            ("DOUBAO_CONFIG", "api_key", "k"),
            ("DOUBAO_CONFIG", "enabled", True),
            ("NETEASE_CLOUD", "cookie", 'has "quotes" inside'),
            ("OOPZ_CONFIG", "proxy", "http://127.0.0.1:7890"),
        ):
            content, ok = deploy.set_config_field(content, section, key, value)
            self.assertTrue(ok, f"{section}.{key} 未写入")

        namespace: dict = {}
        exec(compile(content, "config.py", "exec"), namespace)
        self.assertEqual(namespace["DOUBAO_CONFIG"]["api_key"], "k")
        self.assertTrue(namespace["DOUBAO_CONFIG"]["enabled"])
        self.assertEqual(namespace["NETEASE_CLOUD"]["cookie"], 'has "quotes" inside')
        self.assertEqual(namespace["OOPZ_CONFIG"]["proxy"], "http://127.0.0.1:7890")


class EnvFileTest(unittest.TestCase):
    """start.sh 的 .env 能力并进来之后不能丢。"""

    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self._orig_root = deploy.ROOT
        deploy.ROOT = self.tmp
        self.addCleanup(setattr, deploy, "ROOT", self._orig_root)

    def _write_env(self, text: str) -> None:
        (self.tmp / ".env").write_text(text, encoding="utf-8")

    def test_reads_plain_pairs(self) -> None:
        self._write_env('CLASH_MIXED_PORT=7899\nCLASH_KERNEL_BIN="mihomo"\n')
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("CLASH_MIXED_PORT", None)
            os.environ.pop("CLASH_KERNEL_BIN", None)
            deploy.load_env_files()
            self.assertEqual(os.environ["CLASH_MIXED_PORT"], "7899")
            self.assertEqual(os.environ["CLASH_KERNEL_BIN"], "mihomo", "引号要去掉")

    def test_existing_env_wins(self) -> None:
        """命令行上临时指定的值不该被 .env 盖掉，文档里就是这么用的。"""
        self._write_env("CLASH_AUTO_START=0\n")
        with patch.dict("os.environ", {"CLASH_AUTO_START": "1"}):
            deploy.load_env_files()
            import os

            self.assertEqual(os.environ["CLASH_AUTO_START"], "1")

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        self._write_env("# 注释\n\nFOO_BAR=1\n")
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("FOO_BAR", None)
            deploy.load_env_files()
            self.assertEqual(os.environ["FOO_BAR"], "1")

    def test_missing_file_is_fine(self) -> None:
        deploy.load_env_files()  # 不该抛异常


class ClashFlagTest(unittest.TestCase):
    def test_flag_accepts_common_truthy_spellings(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on"):
            with patch.dict("os.environ", {"X": value}):
                self.assertTrue(deploy.env_flag("X"), value)

    def test_flag_rejects_others(self) -> None:
        for value in ("0", "false", "no", ""):
            with patch.dict("os.environ", {"X": value}):
                self.assertFalse(deploy.env_flag("X"), value)

    def test_kernel_lookup_prefers_configured_binary(self) -> None:
        with (
            patch.dict("os.environ", {"CLASH_KERNEL_BIN": "my-clash"}),
            patch.object(deploy.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}"),
        ):
            self.assertEqual(deploy.find_clash_kernel(), "/usr/bin/my-clash")

    def test_kernel_lookup_falls_back_to_known_names(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(
                deploy.shutil, "which",
                side_effect=lambda n: "/usr/bin/mihomo" if n == "mihomo" else None,
            ),
        ):
            self.assertEqual(deploy.find_clash_kernel(), "/usr/bin/mihomo")

    def test_kernel_missing_returns_none(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(deploy.shutil, "which", return_value=None),
        ):
            self.assertIsNone(deploy.find_clash_kernel())


class StartShRemovedTest(unittest.TestCase):
    def test_start_sh_is_gone_and_unreferenced(self) -> None:
        self.assertFalse((REPO_ROOT / "start.sh").exists())
        for name in ("README.md", "docs/quickstart.md"):
            self.assertNotIn(
                "start.sh",
                (REPO_ROOT / name).read_text(encoding="utf-8"),
                f"{name} still points at the removed script",
            )


class DeadUpstreamTest(unittest.TestCase):
    """上游 git 仓库已因版权清空，不能再靠 clone 装网易云 API。"""

    def test_does_not_clone_the_dead_repo(self) -> None:
        source = (REPO_ROOT / "deploy.py").read_text(encoding="utf-8")

        self.assertNotIn(
            "Binaryify/NeteaseCloudMusicApi.git",
            source,
            "该仓库已清空，clone 下来只有一个 README，装不出可用的服务",
        )

    def test_uses_the_fork_the_docs_use(self) -> None:
        """脚本装的必须和文档教用户装的是同一个，否则两条路会跑偏。"""
        docs = (REPO_ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")

        self.assertIn("api-enhanced", deploy.NETEASE_REPO)
        self.assertIn("api-enhanced", docs)


if __name__ == "__main__":
    unittest.main()
