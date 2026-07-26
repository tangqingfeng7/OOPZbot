"""Web 控制命令的域归属回归测试。

命令队列是全局单键，原先载荷里不带域 —— B 域视图上按「切歌」会把 A 域正在
播的歌切掉。载荷带域后由消费端校验。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from core.redis_keys import decode_web_command, encode_web_command


class EncodeDecodeTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        self.assertEqual(decode_web_command(encode_web_command("A1", "next")), ("A1", "next"))

    def test_empty_area_roundtrips(self) -> None:
        self.assertEqual(decode_web_command(encode_web_command("", "next")), ("", "next"))

    def test_only_first_separator_splits(self) -> None:
        # notify:{json} 的正文里可能带 |，不能被截断
        payload = 'notify:{"name":"a|b","artists":"c"}'
        self.assertEqual(decode_web_command(encode_web_command("A1", payload)), ("A1", payload))

    def test_legacy_payload_without_separator_is_passed_through(self) -> None:
        # 滚动升级期 Redis 里可能残留升级前写入的命令，无从判断归属只能放行
        self.assertEqual(decode_web_command("next"), ("", "next"))

    def test_area_is_stripped(self) -> None:
        self.assertEqual(decode_web_command(encode_web_command("  A1  ", "stop")), ("A1", "stop"))


class CommandAreaGateTest(unittest.TestCase):
    def _handler(self, current_area):
        from music.music import MusicHandler

        handler = MusicHandler.__new__(MusicHandler)  # 跳过 __init__ 的重量级构造
        handler._voice_channel_area = current_area
        return handler

    def test_command_from_another_area_is_skipped(self) -> None:
        handler = self._handler("area-A")
        self.assertFalse(handler._web_command_applies_here("area-B"))

    def test_command_from_the_playing_area_runs(self) -> None:
        handler = self._handler("area-A")
        self.assertTrue(handler._web_command_applies_here("area-A"))

    def test_empty_command_area_is_unrestricted(self) -> None:
        # 后台改的全局默认音量不归属任何域
        handler = self._handler("area-A")
        self.assertTrue(handler._web_command_applies_here(""))

    def test_no_current_area_accepts_everything(self) -> None:
        # 尚未进入任何语音域时不做限制，否则命令会全部落空
        handler = self._handler(None)
        self.assertTrue(handler._web_command_applies_here("area-B"))


class ProducerCarriesAreaTest(unittest.TestCase):
    """execute_control_action 早就收了 area，只是没带进命令载荷。"""

    class _Recorder:
        def __init__(self):
            self.pushed = []

        def rpush(self, key, value):
            self.pushed.append(value)

        def delete(self, *keys):
            return len(keys)

        def set(self, *a, **k):
            return None

    def test_control_actions_tag_the_area(self) -> None:
        from web.web_player import execute_control_action

        for action, body in (
            ("next", {}),
            ("stop", {}),
            ("pause", {}),
            ("resume", {}),
            ("seek", {"position": 12}),
            ("volume", {"volume": 50}),
        ):
            with self.subTest(action=action):
                r = self._Recorder()
                with mock.patch("web.web_player._area_key", side_effect=lambda base, area: base):
                    execute_control_action(action, body, r, area="area-A")

                self.assertTrue(r.pushed, f"{action} 应写入一条命令")
                area, _cmd = decode_web_command(r.pushed[-1])
                self.assertEqual(area, "area-A")


if __name__ == "__main__":
    unittest.main()
