import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from oopz.oopz_upload import (
    RemoteFetchError,
    _download_limited,
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
