import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from core.proxy_utils import is_fake_ip
from oopz.oopz_upload import (
    RemoteFetchError,
    _download_limited,
    _is_public_host,
    _validate_remote_url,
)


class _FakeResponse:
    def __init__(self, chunks, headers=None, status_ok=True):
        self._chunks = chunks
        self.headers = headers or {}
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise RuntimeError("HTTP error")

    def iter_content(self, chunk_size=0):
        yield from self._chunks

    def close(self):
        pass


def _fake_session(response):
    return SimpleNamespace(get=lambda url, **kwargs: response)


class ValidateRemoteUrlTest(unittest.TestCase):
    def test_rejects_non_http_scheme(self) -> None:
        for url in ("file:///etc/passwd", "ftp://x/y", "gopher://x"):
            with self.assertRaises(RemoteFetchError):
                _validate_remote_url(url)

    def test_rejects_missing_host(self) -> None:
        with self.assertRaises(RemoteFetchError):
            _validate_remote_url("http://")

    def test_rejects_loopback_and_private_and_metadata(self) -> None:
        for url in (
            "http://127.0.0.1/x",
            "http://10.0.0.1/x",
            "http://192.168.1.1/x",
            "http://169.254.169.254/latest/meta-data",  # 云元数据端点
        ):
            with self.assertRaises(RemoteFetchError):
                _validate_remote_url(url)

    def test_allows_public_ip(self) -> None:
        # IP 字面量解析不需要联网
        _validate_remote_url("http://8.8.8.8/x.jpg")


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
        with mock.patch.object(
            socket, "getaddrinfo", return_value=_addrinfo("198.18.0.152", "fdfe:dcba:9876::126")
        ):
            self.assertTrue(_is_public_host("p3.music.126.net"))

    def test_allows_host_mixing_fake_ip_and_public(self) -> None:
        with mock.patch.object(socket, "getaddrinfo", return_value=_addrinfo("198.18.0.7", "1.2.3.4")):
            self.assertTrue(_is_public_host("cdn.example.com"))

    def test_still_rejects_host_resolving_to_real_private_address(self) -> None:
        # 占位地址不作数，但同一域名解析出的真实内网地址仍必须拒绝
        with mock.patch.object(socket, "getaddrinfo", return_value=_addrinfo("198.18.0.7", "192.168.1.5")):
            self.assertFalse(_is_public_host("intranet.example.com"))

    def test_rejects_fake_ip_literal(self) -> None:
        # 直接写占位地址时无从反查它对应哪个域名，按内网拒绝
        self.assertFalse(_is_public_host("198.18.0.152"))
        with self.assertRaises(RemoteFetchError):
            _validate_remote_url("http://198.18.0.152/x.jpg")

    def test_ip_literal_skips_dns_entirely(self) -> None:
        def _boom(*_args, **_kwargs):
            raise AssertionError("IP 字面量不应触发 DNS 解析")

        with mock.patch.object(socket, "getaddrinfo", side_effect=_boom):
            self.assertTrue(_is_public_host("8.8.8.8"))
            self.assertFalse(_is_public_host("127.0.0.1"))

    def test_is_fake_ip_classification(self) -> None:
        for addr in ("198.18.0.1", "198.19.255.254", "fdfe:dcba:9876::1"):
            self.assertTrue(is_fake_ip(addr), addr)
        for addr in ("8.8.8.8", "192.168.1.1", "127.0.0.1", "2001:4860:4860::8888", "not-an-ip"):
            self.assertFalse(is_fake_ip(addr), addr)


class DownloadLimitedTest(unittest.TestCase):
    def test_returns_bytes_and_content_type(self) -> None:
        resp = _FakeResponse([b"abc", b"def"], headers={"Content-Type": "image/png"})
        data, ctype = _download_limited(
            _fake_session(resp), "http://8.8.8.8/x.png", max_bytes=1024, timeout=5
        )
        self.assertEqual(data, b"abcdef")
        self.assertEqual(ctype, "image/png")

    def test_rejects_when_streamed_body_exceeds_limit(self) -> None:
        resp = _FakeResponse([b"x" * 100, b"y" * 100])
        with self.assertRaises(RemoteFetchError):
            _download_limited(
                _fake_session(resp), "http://8.8.8.8/big", max_bytes=150, timeout=5
            )

    def test_rejects_when_declared_content_length_exceeds_limit(self) -> None:
        resp = _FakeResponse([b"x"], headers={"Content-Length": "9999999"})
        with self.assertRaises(RemoteFetchError):
            _download_limited(
                _fake_session(resp), "http://8.8.8.8/big", max_bytes=1024, timeout=5
            )

    def test_rejects_private_url_before_fetch(self) -> None:
        resp = _FakeResponse([b"x"])
        with self.assertRaises(RemoteFetchError):
            _download_limited(
                _fake_session(resp), "http://127.0.0.1/x", max_bytes=1024, timeout=5
            )


if __name__ == "__main__":
    unittest.main()
