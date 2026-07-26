import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from web.web_request_context import cookie_secure_for, request_is_https


def _request(scheme="http", proto=None):
    headers = {} if proto is None else {"x-forwarded-proto": proto}
    return SimpleNamespace(headers=headers, url=SimpleNamespace(scheme=scheme))


class RequestIsHttpsTest(unittest.TestCase):
    def test_direct_http_is_not_https(self) -> None:
        self.assertFalse(request_is_https(_request("http")))

    def test_direct_https_is_https(self) -> None:
        self.assertTrue(request_is_https(_request("https")))

    def test_forwarded_proto_overrides_scheme(self) -> None:
        # 反代场景：uvicorn 收到的永远是明文 http，只能信 X-Forwarded-Proto
        self.assertTrue(request_is_https(_request("http", "https")))
        self.assertFalse(request_is_https(_request("https", "http")))

    def test_forwarded_proto_uses_first_hop_case_insensitively(self) -> None:
        self.assertTrue(request_is_https(_request("http", "HTTPS, http")))

    def test_empty_forwarded_proto_falls_back_to_scheme(self) -> None:
        self.assertTrue(request_is_https(_request("https", "")))


class CookieSecureForTest(unittest.TestCase):
    def test_config_off_never_secure(self) -> None:
        self.assertFalse(cookie_secure_for(_request("https"), False))

    def test_config_on_degrades_on_plain_http(self) -> None:
        # 关键回归：HTTP 下不降级会让浏览器拒绝回传 Cookie，登录后立刻被踢回登录页
        self.assertFalse(cookie_secure_for(_request("http"), True))

    def test_config_on_stays_secure_behind_https_proxy(self) -> None:
        self.assertTrue(cookie_secure_for(_request("http", "https"), True))


if __name__ == "__main__":
    unittest.main()
