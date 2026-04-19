import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import types

from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

if "config" not in sys.modules:
    config_module = types.ModuleType("config")
    config_module.DEFAULT_HEADERS = {}
    config_module.AUTO_RECALL_CONFIG = {"enabled": False}
    config_module.OOPZ_CONFIG = {
        "base_url": "https://api.example.com",
        "app_version": "test",
        "channel": "test",
        "device_id": "device",
        "platform": "web",
        "web": True,
        "person_uid": "bot",
        "jwt_token": "jwt",
        "default_area": "area",
        "default_channel": "channel",
        "use_announcement_style": False,
    }
    sys.modules["config"] = config_module


class _FakeStreamingResponse:
    def __init__(self, *, content: bytes, headers: dict[str, str] | None = None, chunks: int = 2):
        self.content = content
        self.headers = headers or {}
        self.status_code = 200
        self._chunks = chunks

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 65536):
        if self._chunks <= 1:
            yield self.content
            return
        step = max(1, len(self.content) // self._chunks)
        for i in range(0, len(self.content), step):
            yield self.content[i : i + step]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ServiceRegressionTest(unittest.TestCase):
    def test_get_person_infos_batch_accepts_id_field(self) -> None:
        from oopz_api import OopzApiMixin

        api = object.__new__(OopzApiMixin)
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "status": True,
            "data": [{"id": "u1", "name": "Alice"}],
        }
        api._post = Mock(return_value=response)

        result = api.get_person_infos_batch(["u1"])

        self.assertEqual(result["u1"]["name"], "Alice")

    def test_send_private_message_v1_uses_flat_payload(self) -> None:
        from oopz_sender import OopzSender

        sender = object.__new__(OopzSender)
        sender.signer = SimpleNamespace(
            client_message_id=lambda: "cid",
            timestamp_us=lambda: "ts",
            oopz_headers=lambda url_path, body_str: {},
        )
        sender.session = SimpleNamespace(headers={}, post=Mock())
        sender._throttle = Mock()
        sender.open_private_session = Mock(return_value={"status": True, "channel": "ch-1"})

        post_response = Mock()
        post_response.status_code = 200
        post_response.text = '{"status":true}'
        post_response.json.return_value = {"status": True}
        sender.session.post.return_value = post_response

        result = sender.send_private_message("u1", "hello", version="v1")

        self.assertTrue(result["status"])
        post_call = sender.session.post.call_args
        self.assertIn("/im/session/v1/sendImMessage", post_call.args[0])
        payload = json.loads(post_call.kwargs["data"].decode("utf-8"))
        self.assertEqual(payload["content"], "hello")
        self.assertNotIn("message", payload)

    def test_send_private_message_v2_keeps_message_wrapper(self) -> None:
        from oopz_sender import OopzSender

        sender = object.__new__(OopzSender)
        sender.signer = SimpleNamespace(
            client_message_id=lambda: "cid",
            timestamp_us=lambda: "ts",
            oopz_headers=lambda url_path, body_str: {},
        )
        sender.session = SimpleNamespace(headers={}, post=Mock())
        sender._throttle = Mock()
        sender.open_private_session = Mock(return_value={"status": True, "channel": "ch-1"})

        post_response = Mock()
        post_response.status_code = 200
        post_response.text = '{"status":true}'
        post_response.json.return_value = {"status": True}
        sender.session.post.return_value = post_response

        sender.send_private_message("u1", "hello")

        post_call = sender.session.post.call_args
        self.assertIn("/im/session/v2/sendImMessage", post_call.args[0])
        payload = json.loads(post_call.kwargs["data"].decode("utf-8"))
        self.assertIn("message", payload)
        self.assertEqual(payload["message"]["content"], "hello")

    def test_upload_file_from_url_stream_true_uses_streaming_download(self) -> None:
        from oopz_upload import UploadMixin

        image = Image.new("RGB", (3, 2), color=(255, 0, 0))
        image_buf = io.BytesIO()
        image.save(image_buf, format="PNG")
        image_bytes = image_buf.getvalue()

        mixin = object.__new__(UploadMixin)
        mixin.session = SimpleNamespace(get=Mock(), put=Mock())
        mixin._put = Mock()
        mixin.session.get.return_value = _FakeStreamingResponse(
            content=image_bytes,
            headers={"Content-Type": "image/png"},
        )

        signed_resp = Mock()
        signed_resp.raise_for_status = Mock()
        signed_resp.json.return_value = {
            "data": {"signedUrl": "https://upload", "file": "file-key", "url": "https://cdn/file-key"}
        }
        mixin._put.return_value = signed_resp

        upload_resp = Mock()
        upload_resp.raise_for_status = Mock()
        mixin.session.put.return_value = upload_resp

        result = mixin.upload_file_from_url("https://example.com/demo.png", stream=True)

        self.assertEqual(result["code"], "success")
        mixin.session.get.assert_called_once()
        self.assertTrue(mixin.session.get.call_args.kwargs["stream"])
        self.assertEqual(result["data"]["fileSize"], len(image_bytes))


if __name__ == "__main__":
    unittest.main()
