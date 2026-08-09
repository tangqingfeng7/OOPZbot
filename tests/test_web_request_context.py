import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from web.web_request_context import (  # noqa: E402
    RequestContext,
    cookie_secure_for,
    request_is_https,
)


def _request(
    scheme: str = "http",
    proto: str | None = None,
    peer: str = "198.51.100.20",
) -> RequestContext:
    headers = {} if proto is None else {"x-forwarded-proto": proto}
    return cast(
        RequestContext,
        SimpleNamespace(
            headers=headers,
            url=SimpleNamespace(scheme=scheme),
            client=SimpleNamespace(host=peer),
        ),
    )


class RequestIsHttpsTest(unittest.TestCase):
    def test_direct_http_is_not_https(self) -> None:
        self.assertFalse(request_is_https(_request("http")))

    def test_direct_https_is_https(self) -> None:
        self.assertTrue(request_is_https(_request("https")))

    def test_trusted_forwarded_proto_overrides_scheme(self) -> None:
        # 反代场景：uvicorn 收到的永远是明文 http，只能信 X-Forwarded-Proto
        trusted = ("127.0.0.1/32",)
        self.assertTrue(
            request_is_https(_request("http", "https", "127.0.0.1"), trusted)
        )
        self.assertFalse(
            request_is_https(_request("https", "http", "127.0.0.1"), trusted)
        )

    def test_untrusted_forwarded_proto_is_ignored(self) -> None:
        self.assertFalse(
            request_is_https(
                _request("http", "https", "198.51.100.20"),
                ("127.0.0.1/32",),
            )
        )

    def test_malformed_forwarded_proto_falls_back_to_transport(self) -> None:
        self.assertFalse(
            request_is_https(
                _request("http", "HTTPS, http", "127.0.0.1"),
                ("127.0.0.1/32",),
            )
        )

    def test_empty_forwarded_proto_falls_back_to_scheme(self) -> None:
        self.assertTrue(request_is_https(_request("https", "")))


class CookieSecureForTest(unittest.TestCase):
    def test_config_off_never_secure(self) -> None:
        self.assertFalse(cookie_secure_for(_request("https"), False))

    def test_config_on_degrades_on_plain_http(self) -> None:
        # 关键回归：HTTP 下不降级会让浏览器拒绝回传 Cookie，登录后立刻被踢回登录页
        self.assertFalse(cookie_secure_for(_request("http"), True))

    def test_config_on_stays_secure_behind_https_proxy(self) -> None:
        self.assertTrue(
            cookie_secure_for(
                _request("http", "https", "127.0.0.1"),
                True,
                ("127.0.0.1/32",),
            )
        )


class WebServerProxyBoundaryTest(unittest.TestCase):
    def test_uvicorn_proxy_header_rewrite_is_disabled(self) -> None:
        import web.web_player as web_player

        server = mock.Mock()
        server.run.return_value = None
        with (
            mock.patch.object(web_player.uvicorn, "Config") as config_factory,
            mock.patch.object(web_player.uvicorn, "Server", return_value=server),
        ):
            service = web_player.WebPlayerService(host="127.0.0.1", port=18080)
            service.start()
            thread = service._thread
            assert thread is not None
            thread.join(timeout=1)
            service.stop(timeout=1)

        self.assertFalse(config_factory.call_args.kwargs["proxy_headers"])


if __name__ == "__main__":
    unittest.main()
