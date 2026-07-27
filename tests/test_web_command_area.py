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

    def test_legacy_payload_without_separator_is_invalid(self) -> None:
        self.assertIsNone(decode_web_command("next"))

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
        self.assertFalse(handler._web_command_applies_here("area-B", "next"))

    def test_command_from_the_playing_area_runs(self) -> None:
        handler = self._handler("area-A")
        self.assertTrue(handler._web_command_applies_here("area-A", "next"))

    def test_empty_command_area_only_allows_volume(self) -> None:
        handler = self._handler("area-A")
        self.assertTrue(handler._web_command_applies_here("", "volume:20"))
        self.assertFalse(handler._web_command_applies_here("", "stop"))

    def test_no_current_area_rejects_area_command(self) -> None:
        handler = self._handler(None)
        self.assertFalse(handler._web_command_applies_here("area-B", "next"))


class ConsumeWebCommandTest(unittest.TestCase):
    """走监听线程真正调用的那一层，光测判定函数抓不到「校验被删掉」。"""

    def _handler(self, current_area):
        from music.music import MusicHandler

        handler = MusicHandler.__new__(MusicHandler)
        handler._voice_channel_area = current_area
        handler._execute_web_command = mock.Mock()
        return handler

    def test_command_from_another_area_is_not_executed(self) -> None:
        handler = self._handler("area-A")

        self.assertFalse(handler._consume_web_command(encode_web_command("area-B", "next")))
        handler._execute_web_command.assert_not_called()

    def test_command_from_the_playing_area_is_executed(self) -> None:
        handler = self._handler("area-A")

        self.assertTrue(handler._consume_web_command(encode_web_command("area-A", "next")))
        handler._execute_web_command.assert_called_once_with("next")

    def test_unscoped_command_is_executed_anywhere(self) -> None:
        # 音量作用于全局唯一的 Agora 输出，不带域
        handler = self._handler("area-A")

        self.assertTrue(handler._consume_web_command(encode_web_command("", "volume:20")))
        handler._execute_web_command.assert_called_once_with("volume:20")

    def test_bytes_payload_is_decoded(self) -> None:
        handler = self._handler("area-A")

        self.assertTrue(handler._consume_web_command(encode_web_command("area-A", "stop").encode()))
        handler._execute_web_command.assert_called_once_with("stop")

    def test_legacy_payload_is_dropped(self) -> None:
        handler = self._handler("area-A")

        self.assertFalse(handler._consume_web_command("next"))
        handler._execute_web_command.assert_not_called()

    def test_area_command_is_dropped_without_current_area(self) -> None:
        handler = self._handler(None)

        self.assertFalse(handler._consume_web_command(encode_web_command("area-B", "stop")))
        handler._execute_web_command.assert_not_called()


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

    def test_track_scoped_actions_tag_the_area(self) -> None:
        """作用在某个域正在播的那首歌上的命令必须带域。"""
        from web.web_player import execute_control_action

        for action, body in (
            ("next", {}),
            ("stop", {}),
            ("pause", {}),
            ("resume", {}),
            ("seek", {"position": 12}),
        ):
            with self.subTest(action=action):
                r = self._Recorder()
                with mock.patch("web.web_player._area_key", side_effect=lambda base, area: base):
                    execute_control_action(action, body, r, area="area-A")

                self.assertTrue(r.pushed, f"{action} 应写入一条命令")
                area, _cmd = decode_web_command(r.pushed[-1])
                self.assertEqual(area, "area-A")

    def test_volume_is_not_area_scoped(self) -> None:
        """音量作用于全局唯一的 Agora 输出设备，带域会让写入生效但命令被丢弃，
        于是 /api/status 读到的值和实际在响的音量长期背离。"""
        from web.web_player import execute_control_action

        r = self._Recorder()
        with mock.patch("web.web_player._area_key", side_effect=lambda base, area: base):
            execute_control_action("volume", {"value": 50}, r, area="area-A")

        area, cmd = decode_web_command(r.pushed[-1])
        self.assertEqual(area, "")
        self.assertEqual(cmd, "volume:50")

    def test_track_control_without_area_is_rejected(self) -> None:
        from web.web_player import execute_control_action

        for action, body in (
            ("next", {}),
            ("clear", {}),
            ("stop", {}),
            ("pause", {}),
            ("resume", {}),
            ("seek", {"time": 12}),
            ("mode", {"value": "loop"}),
        ):
            with self.subTest(action=action):
                r = self._Recorder()
                result = execute_control_action(action, body, r, area="")

                self.assertFalse(result["ok"])
                self.assertEqual(r.pushed, [])


if __name__ == "__main__":
    unittest.main()
