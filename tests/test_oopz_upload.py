import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

from urllib3.util import Timeout

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from core.proxy_utils import is_fake_ip, resolve_proxy_settings  # noqa: E402
from oopz.remote_fetch import (  # noqa: E402
    PublicTargetResolver,
    RemoteFetchError,
    ResolvedTarget,
    SafeRemoteFetcher,
    Urllib3PinnedTransport,
    resolve_via_trusted_dns,
)


class _FakeResponse:
    def __init__(self, chunks, headers=None, status_code=200):
        self._chunks = chunks
        self.headers = headers or {}
        self.status = status_code
        self.released = False

    def stream(self, _chunk_size=0, decode_content=True):
        yield from self._chunks

    def release_conn(self):
        self.released = True


class _FakeTransport:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, target, address, **kwargs):
        self.calls.append((target, address, kwargs))
        return self._responses.pop(0)


def _fetcher(*responses, resolver=None, proxy_value: bool | str = False):
    transport = _FakeTransport(*responses)
    return (
        SafeRemoteFetcher(
            resolver=resolver,
            transport=transport,
            proxy_value=proxy_value,
        ),
        transport,
    )


class _DohResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.closed = False

    def json(self):
        return self._payload

    def close(self):
        self.closed = True


class ValidateRemoteUrlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = PublicTargetResolver()

    def test_rejects_non_http_scheme(self) -> None:
        for url in ("file:///etc/passwd", "ftp://x/y", "gopher://x"):
            with self.assertRaises(RemoteFetchError):
                self.resolver.resolve_url(url)

    def test_rejects_missing_host(self) -> None:
        with self.assertRaises(RemoteFetchError):
            self.resolver.resolve_url("http://")

    def test_rejects_url_credentials(self) -> None:
        with self.assertRaises(RemoteFetchError):
            self.resolver.resolve_url("https://user:password@8.8.8.8/file")

    def test_rejects_non_public_ip_literals(self) -> None:
        for url in (
            "http://127.0.0.1/x",
            "http://10.0.0.1/x",
            "http://192.168.1.1/x",
            "http://169.254.169.254/latest/meta-data",  # 云元数据端点
            "http://[::1]/x",
            "http://[fc00::1]/x",
            "http://[fe80::1]/x",
            "http://[fec0::1]/x",  # 已废弃但仍可能存在内部路由的 site-local
            "http://[64:ff9b::7f00:1]/x",  # NAT64 嵌入 127.0.0.1
            "http://[64:ff9b::a9fe:a9fe]/x",  # NAT64 嵌入 169.254.169.254
            "http://[64:ff9b:1::1]/x",  # 本地使用的 IPv4/IPv6 翻译前缀
        ):
            with self.subTest(url=url), self.assertRaises(RemoteFetchError):
                self.resolver.resolve_url(url)

    def test_allows_public_ip(self) -> None:
        # IP 字面量解析不需要联网
        self.resolver.resolve_url("http://8.8.8.8/x.jpg")


def _addrinfo(*addresses):
    """构造 socket.getaddrinfo 的返回形状，避免测试依赖真实 DNS。"""
    infos = []
    for addr in addresses:
        if ":" in addr:
            infos.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, 0, 0, 0)))
        else:
            infos.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)))
    return infos


class FakeIpHostTest(unittest.TestCase):
    """Clash / mihomo 开 fake-ip 时，本机 DNS 返回占位地址而非真实目标。

    占位段在 ipaddress 里被判为 private，早先会让 SSRF 防护误杀所有域名
    （表现为「目标地址不是公网地址，已拒绝: p3.music.126.net」）。
    """

    def test_allows_host_resolving_only_to_fake_ip(self) -> None:
        resolver = PublicTargetResolver(trusted_dns=lambda _host: ("1.2.3.4",))
        with (
            mock.patch.object(
                socket,
                "getaddrinfo",
                return_value=_addrinfo("198.18.0.152", "fdfe:dcba:9876::126"),
            ),
        ):
            self.assertEqual(resolver.resolve_host("p3.music.126.net"), ("1.2.3.4",))

    def test_allows_host_mixing_fake_ip_and_public(self) -> None:
        resolver = PublicTargetResolver(trusted_dns=lambda _host: ("1.2.3.4",))
        with (
            mock.patch.object(
                socket,
                "getaddrinfo",
                return_value=_addrinfo("198.18.0.7", "1.2.3.4"),
            ),
        ):
            self.assertEqual(resolver.resolve_host("cdn.example.com"), ("1.2.3.4",))

    def test_still_rejects_host_resolving_to_real_private_address(self) -> None:
        # 占位地址不作数，但同一域名解析出的真实内网地址仍必须拒绝
        with (
            mock.patch.object(
                socket,
                "getaddrinfo",
                return_value=_addrinfo("198.18.0.7", "192.168.1.5"),
            ),
            self.assertRaises(RemoteFetchError),
        ):
            PublicTargetResolver().resolve_host("intranet.example.com")

    def test_rejects_mixed_public_and_private_resolution(self) -> None:
        with (
            mock.patch.object(
                socket,
                "getaddrinfo",
                return_value=_addrinfo("8.8.8.8", "10.0.0.8"),
            ),
            self.assertRaises(RemoteFetchError),
        ):
            PublicTargetResolver().resolve_host("mixed.example.com")

    def test_rejects_reserved_nat64_and_site_local_dns_results(self) -> None:
        for address in (
            "64:ff9b::7f00:1",
            "64:ff9b::a9fe:a9fe",
            "64:ff9b:1::1",
            "fec0::1",
        ):
            with (
                self.subTest(address=address),
                mock.patch.object(
                    socket,
                    "getaddrinfo",
                    return_value=_addrinfo(address),
                ),
                self.assertRaises(RemoteFetchError),
            ):
                PublicTargetResolver().resolve_host("internal.example.com")

    def test_rejects_fake_ip_when_trusted_resolution_fails(self) -> None:
        resolver = PublicTargetResolver(trusted_dns=lambda _host: None)
        with (
            mock.patch.object(socket, "getaddrinfo", return_value=_addrinfo("198.18.0.7")),
            self.assertRaises(RemoteFetchError),
        ):
            resolver.resolve_host("unresolved.example.com")

    def test_rejects_fake_ip_whose_real_address_is_private(self) -> None:
        resolver = PublicTargetResolver(trusted_dns=lambda _host: ("192.168.1.5",))
        with (
            mock.patch.object(socket, "getaddrinfo", return_value=_addrinfo("198.18.0.7")),
            self.assertRaises(RemoteFetchError),
        ):
            resolver.resolve_host("intranet.example.com")

    def test_rejects_fake_ip_literal(self) -> None:
        # 直接写占位地址时无从反查它对应哪个域名，按内网拒绝
        with self.assertRaises(RemoteFetchError):
            PublicTargetResolver().resolve_url("http://198.18.0.152/x.jpg")

    def test_ip_literal_skips_dns_entirely(self) -> None:
        def _boom(*_args, **_kwargs):
            raise AssertionError("IP 字面量不应触发 DNS 解析")

        with mock.patch.object(socket, "getaddrinfo", side_effect=_boom):
            self.assertEqual(PublicTargetResolver().resolve_host("8.8.8.8"), ("8.8.8.8",))
            with self.assertRaises(RemoteFetchError):
                PublicTargetResolver().resolve_host("127.0.0.1")

    def test_is_fake_ip_classification(self) -> None:
        for addr in ("198.18.0.1", "198.19.255.254", "fdfe:dcba:9876::1"):
            self.assertTrue(is_fake_ip(addr), addr)
        for addr in ("8.8.8.8", "192.168.1.1", "127.0.0.1", "2001:4860:4860::8888", "not-an-ip"):
            self.assertFalse(is_fake_ip(addr), addr)


class TrustedDnsTest(unittest.TestCase):
    def test_resolves_both_address_families_without_redirects(self) -> None:
        responses = (
            _DohResponse(
                {"Status": 0, "Answer": [{"type": 1, "data": "1.2.3.4"}]}
            ),
            _DohResponse(
                {"Status": 0, "Answer": [{"type": 28, "data": "2001:4860::1"}]}
            ),
        )
        with mock.patch.object(
            sys.modules["oopz.remote_fetch"].requests.Session,
            "get",
            side_effect=responses,
        ) as get:
            result = resolve_via_trusted_dns("cdn.example.com")

        self.assertEqual(result, ("1.2.3.4", "2001:4860::1"))
        self.assertEqual(get.call_count, 2)
        for call in get.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
        self.assertTrue(all(response.closed for response in responses))

    def test_resolution_failure_is_not_permissive(self) -> None:
        response = _DohResponse({"Status": 2})
        with mock.patch.object(
            sys.modules["oopz.remote_fetch"].requests.Session,
            "get",
            return_value=response,
        ):
            self.assertIsNone(resolve_via_trusted_dns("bad.example.com"))
        self.assertTrue(response.closed)


class DownloadLimitedTest(unittest.TestCase):
    def test_returns_bytes_and_content_type(self) -> None:
        resp = _FakeResponse([b"abc", b"def"], headers={"Content-Type": "image/png"})
        fetcher, _transport = _fetcher(resp)
        data, ctype = fetcher.fetch("http://8.8.8.8/x.png", max_bytes=1024, timeout=5)
        self.assertEqual(data, b"abcdef")
        self.assertEqual(ctype, "image/png")
        self.assertTrue(resp.released)

    def test_rejects_when_streamed_body_exceeds_limit(self) -> None:
        resp = _FakeResponse([b"x" * 100, b"y" * 100])
        fetcher, _transport = _fetcher(resp)
        with self.assertRaises(RemoteFetchError):
            fetcher.fetch("http://8.8.8.8/big", max_bytes=150, timeout=5)

    def test_rejects_when_declared_content_length_exceeds_limit(self) -> None:
        resp = _FakeResponse([b"x"], headers={"Content-Length": "9999999"})
        fetcher, _transport = _fetcher(resp)
        with self.assertRaises(RemoteFetchError):
            fetcher.fetch("http://8.8.8.8/big", max_bytes=1024, timeout=5)

    def test_rejects_unhandled_redirect_status(self) -> None:
        resp = _FakeResponse([], status_code=304)
        fetcher, _transport = _fetcher(resp)
        with self.assertRaises(RemoteFetchError):
            fetcher.fetch("http://8.8.8.8/not-modified", max_bytes=1024, timeout=5)

    def test_rejects_private_url_before_fetch(self) -> None:
        resp = _FakeResponse([b"x"])
        fetcher, transport = _fetcher(resp)
        with self.assertRaises(RemoteFetchError):
            fetcher.fetch("http://127.0.0.1/x", max_bytes=1024, timeout=5)
        self.assertEqual(transport.calls, [])

    def test_rejects_redirect_to_private_url_before_second_fetch(self) -> None:
        redirect = _FakeResponse(
            [],
            headers={"Location": "http://127.0.0.1/private"},
            status_code=302,
        )
        private = _FakeResponse([b"secret"])
        fetcher, transport = _fetcher(redirect, private)

        with self.assertRaises(RemoteFetchError):
            fetcher.fetch("http://8.8.8.8/start", max_bytes=1024, timeout=5)

        self.assertEqual(len(transport.calls), 1, "私网重定向不应发出第二次请求")

    def test_pins_validated_address_and_preserves_original_host(self) -> None:
        resp = _FakeResponse([b"ok"])
        resolver = PublicTargetResolver()
        fetcher, transport = _fetcher(resp, resolver=resolver)
        with mock.patch.object(
            socket,
            "getaddrinfo",
            return_value=_addrinfo("8.8.4.4"),
        ) as resolve:
            data, _ctype = fetcher.fetch(
                "https://cdn.example.com:8443/image",
                max_bytes=1024,
                timeout=(2, 5),
            )

        self.assertEqual(data, b"ok")
        self.assertEqual(resolve.call_count, 1)
        target, address, kwargs = transport.calls[0]
        self.assertEqual(address, "8.8.4.4")
        self.assertEqual(target.host, "cdn.example.com")
        self.assertEqual(kwargs["headers"]["Host"], "cdn.example.com:8443")

    def test_does_not_forward_credentials_or_cookies(self) -> None:
        resp = _FakeResponse([b"ok"])
        fetcher, transport = _fetcher(resp)
        fetcher.fetch(
            "https://8.8.8.8/x",
            max_bytes=1024,
            timeout=5,
            headers={
                "Authorization": "secret",
                "Cookie": "session=secret",
                "Referer": "https://music.example/",
            },
        )

        sent = transport.calls[0][2]["headers"]
        self.assertNotIn("Authorization", sent)
        self.assertNotIn("Cookie", sent)
        self.assertEqual(sent["Referer"], "https://music.example/")

    def test_dns_rebinding_cannot_trigger_a_second_resolution(self) -> None:
        resp = _FakeResponse([b"public"])
        fetcher, transport = _fetcher(resp)
        with mock.patch.object(
            socket,
            "getaddrinfo",
            side_effect=(
                _addrinfo("8.8.8.8"),
                _addrinfo("127.0.0.1"),
            ),
        ) as resolve:
            data, _ctype = fetcher.fetch(
                "http://cdn.example.com/x",
                max_bytes=1024,
                timeout=5,
            )

        self.assertEqual(data, b"public")
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(transport.calls[0][1], "8.8.8.8")

    def test_system_proxy_selects_target_scheme(self) -> None:
        resp = _FakeResponse([b"ok"])
        fetcher, transport = _fetcher(resp, proxy_value="")
        with (
            mock.patch("oopz.remote_fetch.proxy_bypass", return_value=False),
            mock.patch(
                "oopz.remote_fetch.getproxies",
                return_value={
                    "http": "http://http-proxy.example:8080",
                    "https": "http://https-proxy.example:8443",
                },
            ),
        ):
            fetcher.fetch(
                "https://8.8.8.8/file",
                max_bytes=1024,
                timeout=5,
            )

        proxy = transport.calls[0][2]["proxy"]
        self.assertEqual(proxy.server, "http://https-proxy.example:8443")

    def test_system_proxy_honors_no_proxy_for_original_host(self) -> None:
        resp = _FakeResponse([b"ok"])
        fetcher, transport = _fetcher(resp, proxy_value="")
        with (
            mock.patch("oopz.remote_fetch.proxy_bypass", return_value=True),
            mock.patch("oopz.remote_fetch.getproxies") as getproxies_mock,
        ):
            fetcher.fetch(
                "https://8.8.8.8/file",
                max_bytes=1024,
                timeout=5,
            )

        self.assertEqual(transport.calls[0][2]["proxy"].mode, "direct")
        getproxies_mock.assert_not_called()


class PinnedTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.target = ResolvedTarget(
            scheme="https",
            host="cdn.example.com",
            port=443,
            path_and_query="/audio?id=1",
            addresses=("8.8.8.8",),
        )
        self.headers = {"Host": "cdn.example.com"}
        self.transport = Urllib3PinnedTransport()

    def test_direct_https_pins_ip_and_preserves_sni_and_certificate_name(self) -> None:
        response = _FakeResponse([b"ok"])
        with mock.patch("oopz.remote_fetch.HTTPSConnectionPool") as pool_factory:
            pool_factory.return_value.request.return_value = response
            result = self.transport.request(
                self.target,
                "8.8.8.8",
                headers=self.headers,
                timeout=Timeout(connect=1, read=1),
                proxy=resolve_proxy_settings(False),
            )

        self.assertIs(result, response)
        pool_factory.assert_called_once_with(
            "8.8.8.8",
            port=443,
            assert_hostname="cdn.example.com",
            server_hostname="cdn.example.com",
            cert_reqs="CERT_REQUIRED",
        )
        request = pool_factory.return_value.request.call_args
        self.assertEqual(request.args[:2], ("GET", "/audio?id=1"))
        self.assertEqual(request.kwargs["headers"]["Host"], "cdn.example.com")

    def test_http_proxy_receives_only_the_validated_ip_target(self) -> None:
        response = _FakeResponse([b"ok"])
        with mock.patch("oopz.remote_fetch.ProxyManager") as manager_factory:
            manager_factory.return_value.request.return_value = response
            result = self.transport.request(
                self.target,
                "8.8.8.8",
                headers=self.headers,
                timeout=Timeout(connect=1, read=1),
                proxy=resolve_proxy_settings("http://proxy.example:8080"),
            )

        self.assertIs(result, response)
        request = manager_factory.return_value.request.call_args
        self.assertEqual(
            request.args[:2],
            ("GET", "https://8.8.8.8:443/audio?id=1"),
        )
        self.assertEqual(request.kwargs["headers"]["Host"], "cdn.example.com")
        manager_factory.assert_called_once_with(
            "http://proxy.example:8080",
            proxy_headers=None,
            assert_hostname="cdn.example.com",
            server_hostname="cdn.example.com",
            cert_reqs="CERT_REQUIRED",
        )

    def test_socks_proxy_receives_only_the_validated_ip_target(self) -> None:
        response = _FakeResponse([b"ok"])
        with mock.patch("oopz.remote_fetch.SOCKSProxyManager") as manager_factory:
            manager_factory.return_value.request.return_value = response
            result = self.transport.request(
                self.target,
                "8.8.8.8",
                headers=self.headers,
                timeout=Timeout(connect=1, read=1),
                proxy=resolve_proxy_settings("socks5://proxy.example:1080"),
            )

        self.assertIs(result, response)
        request = manager_factory.return_value.request.call_args
        self.assertEqual(
            request.args[:2],
            ("GET", "https://8.8.8.8:443/audio?id=1"),
        )
        self.assertEqual(request.kwargs["headers"]["Host"], "cdn.example.com")
        manager_factory.assert_called_once_with(
            "socks5://proxy.example:1080",
            username=None,
            password=None,
            assert_hostname="cdn.example.com",
            server_hostname="cdn.example.com",
            cert_reqs="CERT_REQUIRED",
        )


if __name__ == "__main__":
    unittest.main()
