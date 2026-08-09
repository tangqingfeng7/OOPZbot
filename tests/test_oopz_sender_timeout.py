"""守卫：所有出站 HTTP 请求都必须带 timeout。

没有超时时服务端挂住会无限阻塞调用线程 —— dispatcher 只有 4 个 worker，
卡满即全线失联，AreaJoinPoll 同样会停摆。
"""

import ast
import sys
import threading
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from core.http_constants import HTTP_TIMEOUT_API, HTTP_TIMEOUT_API_SLOW  # noqa: E402

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
    from requests import Session

    from oopz.oopz_sender import OopzSender, Signer

    sender = OopzSender.__new__(OopzSender)
    session = _RecordingSession()
    sender.signer = cast(Signer, _Signer())
    sender.session = cast(Session, session)
    sender._auth_refresh_lock = threading.Lock()
    return sender, session


class SignedRequestTimeoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sender, self.session = _make_sender()
        self.sender._throttle = lambda: None

    def test_write_requests_carry_the_default_tier(self) -> None:
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                self.session.calls.clear()
                self.sender._signed_request_once(method, "/x", {"a": 1})

                self.assertEqual(self.session.calls[0]["timeout"], HTTP_TIMEOUT_API)

    def test_get_carries_the_default_tier(self) -> None:
        self.sender._get_once("/x", params={"a": "1"})

        self.assertEqual(self.session.calls[0]["timeout"], HTTP_TIMEOUT_API)

    def test_caller_can_override_with_a_slower_tier(self) -> None:
        self.sender._signed_request_once("POST", "/x", {"a": 1}, timeout=HTTP_TIMEOUT_API_SLOW)

        self.assertEqual(self.session.calls[0]["timeout"], HTTP_TIMEOUT_API_SLOW)

    def test_timeout_reaches_transport_through_post_put_delete(self) -> None:
        self.sender._AUTH_REFRESH_STATUSES = set()
        for call in (
            lambda: self.sender._post("/x", {}, timeout=HTTP_TIMEOUT_API_SLOW),
            lambda: self.sender._put("/x", {}, timeout=HTTP_TIMEOUT_API_SLOW),
            lambda: self.sender._delete("/x", timeout=HTTP_TIMEOUT_API_SLOW),
        ):
            self.session.calls.clear()
            call()
            self.assertEqual(self.session.calls[0]["timeout"], HTTP_TIMEOUT_API_SLOW)


class SlowTierPassthroughTest(unittest.TestCase):
    """确有批量语义的接口要走慢档，且 timeout 能从业务方法穿透到传输层。

    只断言常数分档没有意义 —— 得确认它真的被那三个接口用上了。
    """

    def _api(self):
        from oopz.oopz_api import OopzApiMixin

        class _Api(OopzApiMixin):
            def __init__(self):
                self.sent = []

            def _get(self, path, params=None, *, timeout=None):
                self.sent.append({"path": path, "timeout": timeout})
                return _Resp()

            def _request(self, method, path, body=None, *, timeout=None):
                self.sent.append({"path": path, "timeout": timeout})
                return _Resp()

        return _Api()

    def test_timeout_passes_through_query_and_mutation(self) -> None:
        api = self._api()

        api._query("POST", "/x", body={}, timeout=HTTP_TIMEOUT_API_SLOW)
        api._mutation("act", "POST", "/y", body={}, timeout=HTTP_TIMEOUT_API_SLOW)

        self.assertEqual(
            [c["timeout"] for c in api.sent],
            [HTTP_TIMEOUT_API_SLOW, HTTP_TIMEOUT_API_SLOW],
        )

    def test_default_is_none_so_transport_picks_the_default_tier(self) -> None:
        api = self._api()
        api._query("GET", "/x")

        self.assertIsNone(api.sent[0]["timeout"], "不传时应交给传输层用默认档，而不是在这里写死")

    def test_batch_endpoints_use_the_slow_tier(self) -> None:
        import config

        api = self._api()
        api.get_user_area_detail = lambda *a, **k: {}
        with mock.patch.dict(config.OOPZ_CONFIG, {"default_area": "A1"}, clear=False):
            api.get_person_infos_batch(["u1"])
            api.search_area_members(area="A1", keyword="x")
            api.get_area_members(area="A1")

        slow = [c for c in api.sent if c["timeout"] == HTTP_TIMEOUT_API_SLOW]
        self.assertEqual(len(slow), 3, f"三个批量接口都应走慢档，实际: {api.sent}")


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


class AutoRecallSchedulingTest(unittest.TestCase):
    def test_sender_delegates_to_shared_scheduler(self) -> None:
        import oopz.oopz_sender as module

        sender = module.OopzSender.__new__(module.OopzSender)
        sender._auto_recall_scheduler = mock.Mock()
        sender._auto_recall_scheduler.schedule_recall.return_value = True
        sender._auto_recall_unbound_warned = False
        response = mock.Mock()
        response.json.return_value = {"data": {"messageId": "message-1"}}

        with mock.patch.object(
            module,
            "AUTO_RECALL_CONFIG",
            {"enabled": True, "delay": 12},
        ):
            sender._schedule_auto_recall(response, "area-1", "channel-1")

        sender._auto_recall_scheduler.schedule_recall.assert_called_once_with(
            message_id="message-1",
            channel="channel-1",
            area="area-1",
            delay=12.0,
        )


if __name__ == "__main__":
    unittest.main()
