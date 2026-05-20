import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _response(message_id: str = "oopz-msg-2", timestamp: str = "1770000000000000"):
    return SimpleNamespace(json=lambda: {"status": True, "data": {"messageId": message_id, "timestamp": timestamp}})


def _raw_message(content: str = "hello") -> dict:
    return {
        "event": 9,
        "body": json.dumps({
            "data": {
                "person": "user-1",
                "area": "area-1",
                "channel": "channel-1",
                "messageId": "oopz-msg-1",
                "timestamp": "1770000000000000",
                "content": content,
            }
        }),
    }


def _raw_friend_request() -> dict:
    return {
        "event": 2,
        "body": json.dumps({
            "data": {
                "person": "friend-1",
                "name": "新朋友",
                "avatar": "https://example.test/avatar.png",
                "type": "apply",
                "friendRequestId": 4455,
                "createTime": "1770000000000000",
            }
        }),
    }


class OneBotV11AdapterTest(unittest.TestCase):
    def _adapter(self, tmpdir: str, **kwargs):
        from onebot_v11.adapter import OneBotV11Adapter

        sender = Mock()
        sender.send_message.return_value = _response()
        sender.send_private_message.return_value = {
            "status": True,
            "channel": "private-channel-1",
            "result": {"data": {"messageId": "private-msg-1", "timestamp": "1770000000000000"}},
        }
        sender.recall_message.return_value = {"status": True}
        sender.recall_private_message.return_value = {"status": True}
        sender.get_joined_areas.return_value = [{"id": "area-1", "name": "测试域"}]
        sender.get_area_channels.return_value = [
            {"channels": [{"id": "channel-1", "name": "公屏", "type": "TEXT"}]}
        ]
        sender.get_friendship.return_value = [{"uid": "friend-1", "name": "好友一", "online": True}]
        sender.post_friendship_response.return_value = {"status": True}
        sender.set_user_remark_name.return_value = {"status": True}
        sender.leave_area.return_value = {"status": True}
        sender.get_channel_setting_info.return_value = {"name": "公屏"}
        sender.get_person_detail_full.return_value = {"name": "用户一", "memberLevel": 3}
        sender.get_user_area_detail.return_value = {"roleName": "成员"}
        sender.get_self_detail.return_value = {"name": "机器人"}
        return OneBotV11Adapter(
            sender,
            self_oopz_id="bot-uid",
            db_path=str(Path(tmpdir) / "onebot.sqlite3"),
            **kwargs,
        ), sender

    def test_id_mapping_is_stable(self) -> None:
        from onebot_v11.store import OneBotStore, make_group_source

        with tempfile.TemporaryDirectory() as tmpdir:
            store = OneBotStore(Path(tmpdir) / "ids.sqlite3")
            first = store.create_id(make_group_source(area="area-1", channel="channel-1"))
            second = store.create_id(make_group_source(area="area-1", channel="channel-1"))

        self.assertEqual(first.number, second.number)
        self.assertEqual(first.source, "group:area-1:channel-1")

    def test_message_event_converts_to_group_event_and_saves_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, _sender = self._adapter(tmpdir)
            event = asyncio.run(adapter.emit_raw_event(_raw_message("hello")))
            stored = adapter.store.get_message(event["message_id"])

        self.assertEqual(event["post_type"], "message")
        self.assertEqual(event["message_type"], "group")
        self.assertEqual(event["raw_message"], "hello")
        self.assertEqual(event["extra"]["oopz_area_id"], "area-1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.oopz_message_id, "oopz-msg-1")

    def test_unknown_event_is_forwarded_as_meta_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, _sender = self._adapter(tmpdir)
            event = asyncio.run(adapter.emit_raw_event({"event": 123, "body": "{}"}))

        self.assertEqual(event["post_type"], "meta_event")
        self.assertEqual(event["meta_event_type"], "oopz")
        self.assertEqual(event["oopz_event_type"], 123)

    def test_friend_request_event_converts_to_onebot_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, _sender = self._adapter(tmpdir)
            event = asyncio.run(adapter.emit_raw_event(_raw_friend_request()))

        self.assertEqual(event["post_type"], "request")
        self.assertEqual(event["request_type"], "friend")
        self.assertEqual(event["comment"], "新朋友")
        self.assertEqual(event["flag"], "oopz_friend_request:4455:friend-1")

    def test_send_group_msg_uses_group_mapping_and_sender(self) -> None:
        from onebot_v11.store import make_group_source

        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, sender = self._adapter(tmpdir)
            group_id = adapter.store.create_id(make_group_source(area="area-1", channel="channel-1")).number
            result = asyncio.run(adapter.call_action("send_group_msg", {
                "group_id": group_id,
                "message": "hi",
            }))
            stored = adapter.store.get_message(result["data"]["message_id"])

        self.assertEqual(result["status"], "ok")
        self.assertIn("message_id", result["data"])
        self.assertEqual(stored.raw["message"], [{"type": "text", "data": {"text": "hi"}}])
        sender.send_message.assert_called_once()
        self.assertEqual(sender.send_message.call_args.kwargs["area"], "area-1")
        self.assertEqual(sender.send_message.call_args.kwargs["channel"], "channel-1")

    def test_send_group_msg_accepts_oopz_context_when_group_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, sender = self._adapter(tmpdir)
            result = asyncio.run(adapter.call_action("send_group_msg", {
                "group_id": 123456,
                "oopz_area_id": "area-1",
                "oopz_channel_id": "channel-1",
                "message": "hi",
            }))

        self.assertEqual(result["status"], "ok")
        sender.send_message.assert_called_once()

    def test_send_group_msg_auto_escape_keeps_cq_code_as_text(self) -> None:
        from onebot_v11.store import make_group_source

        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, sender = self._adapter(tmpdir)
            group_id = adapter.store.create_id(make_group_source(area="area-1", channel="channel-1")).number
            result = asyncio.run(adapter.call_action("send_group_msg", {
                "group_id": group_id,
                "message": "[CQ:at,qq=123] hello",
                "auto_escape": True,
            }))
            stored = adapter.store.get_message(result["data"]["message_id"])

        self.assertEqual(result["status"], "ok")
        self.assertEqual(sender.send_message.call_args.args[0], "[CQ:at,qq=123] hello")
        self.assertEqual(sender.send_message.call_args.kwargs["mentionList"], [])
        self.assertEqual(stored.raw["message"], [
            {"type": "text", "data": {"text": "[CQ:at,qq=123] hello"}}
        ])

    def test_delete_msg_uses_saved_message_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, sender = self._adapter(tmpdir)
            event = asyncio.run(adapter.emit_raw_event(_raw_message("hello")))
            result = asyncio.run(adapter.call_action("delete_msg", {"message_id": event["message_id"]}))

        self.assertEqual(result["status"], "ok")
        sender.recall_message.assert_called_once_with(
            "oopz-msg-1",
            area="area-1",
            channel="channel-1",
            target="",
        )

    def test_delete_private_msg_uses_private_recall(self) -> None:
        from onebot_v11.store import make_user_source

        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, sender = self._adapter(tmpdir)
            user_id = adapter.store.create_id(make_user_source("friend-1")).number
            send_result = asyncio.run(adapter.call_action("send_private_msg", {
                "user_id": user_id,
                "message": "hi",
            }))
            result = asyncio.run(adapter.call_action("delete_msg", {
                "message_id": send_result["data"]["message_id"],
            }))

        self.assertEqual(result["status"], "ok")
        sender.recall_private_message.assert_called_once_with(
            "private-msg-1",
            channel="private-channel-1",
            target="friend-1",
            area=None,
        )

    def test_get_group_list_maps_area_channels_to_groups(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, _sender = self._adapter(tmpdir)
            result = asyncio.run(adapter.call_action("get_group_list", {}))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"][0]["group_name"], "公屏")
        self.assertEqual(result["data"][0]["extra"]["oopz_area_id"], "area-1")

    def test_get_friend_list_maps_friends(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, _sender = self._adapter(tmpdir)
            result = asyncio.run(adapter.call_action("get_friend_list", {}))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"][0]["nickname"], "好友一")
        self.assertEqual(result["data"][0]["extra"]["oopz_user_id"], "friend-1")

    def test_set_friend_add_request_accepts_and_sets_remark(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, sender = self._adapter(tmpdir)
            result = asyncio.run(adapter.call_action("set_friend_add_request", {
                "flag": "oopz_friend_request:4455:friend-1",
                "approve": True,
                "remark": "备注",
            }))

        self.assertEqual(result["status"], "ok")
        sender.post_friendship_response.assert_called_once_with("friend-1", 4455, True)
        sender.set_user_remark_name.assert_called_once_with("friend-1", "备注")

    def test_set_group_leave_is_available_when_enabled(self) -> None:
        from onebot_v11.store import make_group_source

        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, sender = self._adapter(tmpdir, enable_set_group_leave_as_area_leave=True)
            group_id = adapter.store.create_id(make_group_source(area="area-1", channel="channel-1")).number
            result = asyncio.run(adapter.call_action("set_group_leave", {"group_id": group_id}))

        self.assertEqual(result["status"], "ok")
        sender.leave_area.assert_called_once_with("area-1")


class OneBotV11ServerTest(unittest.TestCase):
    def _adapter(self, tmpdir: str):
        from onebot_v11.adapter import OneBotV11Adapter

        sender = Mock()
        return OneBotV11Adapter(sender, self_oopz_id="bot-uid", db_path=str(Path(tmpdir) / "server.sqlite3"))

    def test_http_auth_and_get_status(self) -> None:
        async def scenario() -> None:
            from aiohttp import ClientSession
            from onebot_v11.config import OneBotV11ServerConfig
            from onebot_v11.server import OneBotV11Server

            with tempfile.TemporaryDirectory() as tmpdir:
                adapter = self._adapter(tmpdir)
                server = OneBotV11Server(
                    adapter,
                    OneBotV11ServerConfig(enabled=True, host="127.0.0.1", port=0, access_token="token"),
                )
                await server.start()
                try:
                    async with ClientSession() as session:
                        async with session.get(f"http://127.0.0.1:{server.bound_port}/get_status") as resp:
                            self.assertEqual(resp.status, 401)
                        async with session.get(
                            f"http://127.0.0.1:{server.bound_port}/get_status",
                            headers={"Authorization": "Bearer token"},
                        ) as resp:
                            self.assertEqual(resp.status, 200)
                            data = await resp.json()
                            self.assertEqual(data["status"], "ok")
                            self.assertTrue(data["data"]["good"])
                finally:
                    await server.stop()

        asyncio.run(scenario())

    def test_forward_websocket_receives_events(self) -> None:
        async def scenario() -> None:
            from aiohttp import ClientSession, WSMsgType
            from onebot_v11.config import OneBotV11ServerConfig
            from onebot_v11.server import OneBotV11Server

            with tempfile.TemporaryDirectory() as tmpdir:
                adapter = self._adapter(tmpdir)
                server = OneBotV11Server(
                    adapter,
                    OneBotV11ServerConfig(enabled=True, host="127.0.0.1", port=0, send_connect_event=False),
                )
                await server.start()
                try:
                    async with ClientSession() as session:
                        ws = await session.ws_connect(f"http://127.0.0.1:{server.bound_port}/event")
                        await adapter.emit_raw_event(_raw_message("ws hello"))
                        msg = await asyncio.wait_for(ws.receive(), timeout=3)
                        self.assertEqual(msg.type, WSMsgType.TEXT)
                        payload = json.loads(msg.data)
                        self.assertEqual(payload["raw_message"], "ws hello")
                        await ws.close()
                finally:
                    await server.stop()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
