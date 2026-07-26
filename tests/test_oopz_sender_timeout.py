"""守卫：所有出站 HTTP 请求都必须带 timeout。

没有超时时服务端挂住会无限阻塞调用线程 —— dispatcher 只有 4 个 worker，
卡满即全线失联，AreaJoinPoll 同样会停摆。
"""

import ast
import sys
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from core.http_constants import HTTP_TIMEOUT_API, HTTP_TIMEOUT_API_SLOW

_HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head", "request"}


class _Resp:
    status_code = 200
    text = "{}"


class _RecordingSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append({"method": method, "timeout": timeout})
        return _Resp()

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"method": "GET", "timeout": timeout})
        return _Resp()


class _Signer:
    def oopz_headers(self, url_path, body_str):
        return {}


def _make_sender():
    from oopz.oopz_sender import OopzSender

    sender = OopzSender.__new__(OopzSender)
    sender.signer = _Signer()
    sender.session = _RecordingSession()
    sender._auth_refresh_lock = threading.Lock()
    return sender


class SignedRequestTimeoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sender = _make_sender()
        self.sender._throttle = lambda: None

    def test_write_requests_carry_the_default_tier(self) -> None:
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                self.sender.session.calls.clear()
                self.sender._signed_request_once(method, "/x", {"a": 1})

                self.assertEqual(self.sender.session.calls[0]["timeout"], HTTP_TIMEOUT_API)

    def test_get_carries_the_default_tier(self) -> None:
        self.sender._get_once("/x", params={"a": "1"})

        self.assertEqual(self.sender.session.calls[0]["timeout"], HTTP_TIMEOUT_API)

    def test_caller_can_override_with_a_slower_tier(self) -> None:
        self.sender._signed_request_once("POST", "/x", {"a": 1}, timeout=HTTP_TIMEOUT_API_SLOW)

        self.assertEqual(self.sender.session.calls[0]["timeout"], HTTP_TIMEOUT_API_SLOW)

    def test_timeout_reaches_transport_through_post_put_delete(self) -> None:
        self.sender._AUTH_REFRESH_STATUSES = ()
        for call in (
            lambda: self.sender._post("/x", {}, timeout=HTTP_TIMEOUT_API_SLOW),
            lambda: self.sender._put("/x", {}, timeout=HTTP_TIMEOUT_API_SLOW),
            lambda: self.sender._delete("/x", timeout=HTTP_TIMEOUT_API_SLOW),
        ):
            self.sender.session.calls.clear()
            call()
            self.assertEqual(self.sender.session.calls[0]["timeout"], HTTP_TIMEOUT_API_SLOW)


class NoTimeoutlessOutboundCallTest(unittest.TestCase):
    """AST 守卫：src/ 下任何 session/requests 的出站调用都不能漏 timeout。"""

    def test_every_outbound_call_passes_timeout(self) -> None:
        offenders = []
        for path in SRC_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute) or func.attr not in _HTTP_VERBS:
                    continue
                receiver = ast.unparse(func.value)
                if "session" not in receiver and "requests" not in receiver:
                    continue
                if not any(kw.arg == "timeout" for kw in node.keywords):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

        self.assertEqual(offenders, [], f"这些出站调用没有 timeout: {offenders}")


class TimeoutTierTest(unittest.TestCase):
    def test_tiers_are_connect_read_tuples(self) -> None:
        for tier in (HTTP_TIMEOUT_API, HTTP_TIMEOUT_API_SLOW):
            self.assertIsInstance(tier, tuple)
            self.assertEqual(len(tier), 2)

    def test_slow_tier_reads_longer_but_connects_the_same(self) -> None:
        # 连接超时与传输量无关，各档共用；分档只分「读」
        self.assertEqual(HTTP_TIMEOUT_API[0], HTTP_TIMEOUT_API_SLOW[0])
        self.assertGreater(HTTP_TIMEOUT_API_SLOW[1], HTTP_TIMEOUT_API[1])


if __name__ == "__main__":
    unittest.main()
