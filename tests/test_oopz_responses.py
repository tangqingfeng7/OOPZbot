"""parse_api_response 的分支等价性测试。

锁定归一化逻辑与各 API 方法内联样板逐字等价，作为请求层收口（总纲 #3）的安全网。
"""

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


class _FakeResponse:
    """最小化 requests.Response 替身。"""

    def __init__(self, status_code=200, content=b"{}", json_value=None, json_raises=False, text=""):
        self.status_code = status_code
        self.content = content
        self.text = text
        self._json_value = json_value if json_value is not None else {}
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("not json")
        return self._json_value


class ParseApiResponseTest(unittest.TestCase):
    def _parse(self, *args, **kwargs):
        from oopz.responses import parse_api_response

        return parse_api_response(*args, **kwargs)

    def test_none_response(self) -> None:
        res = self._parse(None)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "未获得响应")
        self.assertIsNone(res.status_code)

    def test_non_200_uses_http_error(self) -> None:
        from oopz.responses import http_error

        res = self._parse(_FakeResponse(status_code=404))
        self.assertFalse(res.ok)
        self.assertEqual(res.error, http_error(404))
        self.assertEqual(res.status_code, 404)

    def test_empty_body(self) -> None:
        res = self._parse(_FakeResponse(status_code=200, content=b""))
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "empty response")
        self.assertEqual(res.status_code, 200)

    def test_invalid_json(self) -> None:
        res = self._parse(_FakeResponse(status_code=200, content=b"<html>", json_raises=True))
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "invalid JSON")

    def test_status_false_prefers_message(self) -> None:
        res = self._parse(_FakeResponse(json_value={"status": False, "message": "msg", "error": "err"}))
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "msg")
        self.assertEqual(res.raw, {"status": False, "message": "msg", "error": "err"})

    def test_status_false_falls_back_to_error_then_default(self) -> None:
        res = self._parse(_FakeResponse(json_value={"status": False, "error": "err"}))
        self.assertEqual(res.error, "err")
        res2 = self._parse(_FakeResponse(json_value={"status": False}))
        self.assertEqual(res2.error, "未知错误")

    def test_success_extracts_data_key(self) -> None:
        res = self._parse(_FakeResponse(json_value={"status": True, "data": {"a": 1}}))
        self.assertTrue(res.ok)
        self.assertEqual(res.data, {"a": 1})
        self.assertIsNone(res.error)

    def test_success_custom_data_key_and_default(self) -> None:
        res = self._parse(
            _FakeResponse(json_value={"status": True}),
            data_key="items",
            data_default=[],
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.data, [])


class ParseMutationResponseTest(unittest.TestCase):
    def _parse(self, *args, **kwargs):
        from oopz.responses import parse_mutation_response

        return parse_mutation_response(*args, **kwargs)

    def test_non_200_includes_body(self) -> None:
        from oopz.responses import http_error

        out = self._parse(_FakeResponse(status_code=500, text="boom"))
        self.assertFalse(out.ok)
        self.assertEqual(out.error, http_error(500, "boom"))

    def test_non_200_respects_body_limit(self) -> None:
        from oopz.responses import http_error

        out = self._parse(_FakeResponse(status_code=400, text="x" * 300), body_limit=150)
        self.assertEqual(out.error, http_error(400, "x" * 300, 150))

    def test_non_json(self) -> None:
        out = self._parse(_FakeResponse(status_code=200, text="<html>", json_raises=True))
        self.assertFalse(out.ok)
        self.assertEqual(out.error, "响应非 JSON: <html>")

    def test_status_true_success(self) -> None:
        out = self._parse(_FakeResponse(json_value={"status": True, "message": "ok"}))
        self.assertTrue(out.ok)
        self.assertEqual(out.server_message, "ok")

    def test_status_missing_is_failure_without_accept_code(self) -> None:
        out = self._parse(_FakeResponse(json_value={"code": 0}))
        self.assertFalse(out.ok)

    def test_accept_code_success(self) -> None:
        for code in (0, "0", "success", 200):
            out = self._parse(_FakeResponse(json_value={"code": code}), accept_code=True)
            self.assertTrue(out.ok, f"code={code!r} 应判成功")

    def test_failure_error_extraction(self) -> None:
        out = self._parse(_FakeResponse(json_value={"status": False, "error": "denied"}))
        self.assertEqual(out.error, "denied")
        out2 = self._parse(_FakeResponse(json_value={"status": False}))
        self.assertEqual(out2.error, str({"status": False}))


if __name__ == "__main__":
    unittest.main()
