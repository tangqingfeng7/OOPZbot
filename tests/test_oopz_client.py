import base64
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from oopz.oopz_client import (  # noqa: E402
    OopzClient,
    _auth_response_failed,
    _jwt_expires_in,
)


def _fake_jwt(payload: dict) -> str:
    def _b64(obj) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")
        return raw.rstrip("=")

    return f"{_b64({'alg': 'RS256'})}.{_b64(payload)}.sig"


def _fake_ws(connected: bool = True):
    return SimpleNamespace(
        sock=SimpleNamespace(connected=connected),
        close=Mock(),
        send=Mock(),
    )


class JwtExpiryParseTest(unittest.TestCase):
    def test_expired_token_returns_negative(self) -> None:
        token = _fake_jwt({"exp": int(time.time()) - 3600})
        result = _jwt_expires_in(token)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertLess(result, 0)

    def test_valid_token_returns_positive(self) -> None:
        token = _fake_jwt({"exp": int(time.time()) + 3600})
        result = _jwt_expires_in(token)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertGreater(result, 3000)

    def test_token_without_exp_returns_none(self) -> None:
        self.assertIsNone(_jwt_expires_in(_fake_jwt({"sub": "u1"})))

    def test_garbage_token_returns_none(self) -> None:
        self.assertIsNone(_jwt_expires_in("not-a-jwt"))
        self.assertIsNone(_jwt_expires_in(""))
        # 故意越过公开类型契约，验证运行时对旧调用方传入 None 仍会安全失败。
        self.assertIsNone(_jwt_expires_in(cast(str, None)))


class AuthResponseFailureDetectionTest(unittest.TestCase):
    def test_explicit_failure_markers(self) -> None:
        self.assertTrue(_auth_response_failed({"success": False}))
        self.assertTrue(_auth_response_failed({"status": False}))
        self.assertTrue(_auth_response_failed({"ok": False}))
        self.assertTrue(_auth_response_failed({"error": "token expired"}))
        self.assertTrue(_auth_response_failed({"code": 401}))
        self.assertTrue(_auth_response_failed({"code": "TOKEN_EXPIRED"}))

    def test_success_and_unknown_shapes_do_not_fail(self) -> None:
        self.assertFalse(_auth_response_failed({}))
        self.assertFalse(_auth_response_failed({"code": "success"}))
        self.assertFalse(_auth_response_failed({"code": 0}))
        self.assertFalse(_auth_response_failed({"code": "200"}))
        self.assertFalse(_auth_response_failed({"r": 1}))
        self.assertFalse(_auth_response_failed({"person": "u1"}))
        # 负向健壮性测试：保留非字典运行时输入，但明确标注为测试越界。
        invalid_body = cast(dict[object, object], "not a dict")
        self.assertFalse(_auth_response_failed(invalid_body))


class StaleConnectionDetectionTest(unittest.TestCase):
    def _client(self, **kwargs) -> OopzClient:
        return OopzClient(stale_connection_timeout=kwargs.pop("stale", 90.0), **kwargs)

    def test_stale_when_no_data_beyond_threshold(self) -> None:
        client = self._client(stale=1.0)
        client._last_recv_time = time.time() - 5
        self.assertTrue(client._connection_is_stale())

    def test_not_stale_with_recent_data(self) -> None:
        client = self._client(stale=30.0)
        client._last_recv_time = time.time()
        self.assertFalse(client._connection_is_stale())

    def test_disabled_when_timeout_not_positive(self) -> None:
        client = self._client(stale=0)
        client._last_recv_time = time.time() - 3600
        self.assertFalse(client._connection_is_stale())

    def test_heartbeat_loop_closes_stale_connection(self) -> None:
        client = self._client(stale=0.05, heartbeat_interval=0.01)
        client._running = True
        client._last_recv_time = time.time() - 10
        ws = _fake_ws()
        connection_stop = threading.Event()

        loop = threading.Thread(
            target=client._heartbeat_loop,
            args=(ws, connection_stop),
            daemon=True,
        )
        loop.start()
        loop.join(timeout=2)

        self.assertFalse(loop.is_alive(), "心跳循环应在判定连接失效后退出")
        ws.close.assert_called_once()
        ws.send.assert_called()

    def test_heartbeat_loop_keeps_healthy_connection(self) -> None:
        client = self._client(stale=60.0, heartbeat_interval=0.01)
        client._running = True
        client._last_recv_time = time.time()
        ws = _fake_ws()
        connection_stop = threading.Event()

        loop = threading.Thread(
            target=client._heartbeat_loop,
            args=(ws, connection_stop),
            daemon=True,
        )
        loop.start()
        time.sleep(0.1)
        client._running = False
        connection_stop.set()
        loop.join(timeout=2)

        ws.close.assert_not_called()

    def test_reconnect_stops_and_tracks_every_heartbeat_worker(self) -> None:
        client = self._client(stale=60.0, heartbeat_interval=60.0)
        client._running = True
        first_ws = _fake_ws()
        second_ws = _fake_ws()

        client._on_open(first_ws)
        with client._heartbeat_lock:
            first_thread = next(iter(client._heartbeat_workers))
        client._on_open(second_ws)

        first_thread.join(timeout=1)
        self.assertFalse(first_thread.is_alive(), "重连后旧心跳线程应立即退出")

        client.stop(timeout=1)
        with client._heartbeat_lock:
            remaining = tuple(client._heartbeat_workers)
        self.assertFalse(any(thread.is_alive() for thread in remaining))


class StopBudgetTest(unittest.TestCase):
    @staticmethod
    def _assert_bounded_stop(close_method: Any) -> tuple[float, threading.Event]:
        client = OopzClient()
        client._ws = cast(Any, SimpleNamespace(close=close_method))
        started = time.monotonic()
        client.stop(timeout=0.01)
        return time.monotonic() - started, close_method.started

    def test_slow_close_receives_remaining_timeout_without_blocking_stop(self) -> None:
        class SlowClose:
            def __init__(self) -> None:
                self.started = threading.Event()
                self.release = threading.Event()
                self.timeout = None

            def __call__(self, *, timeout: float) -> None:
                self.timeout = timeout
                self.started.set()
                self.release.wait(timeout=1)

        close = SlowClose()
        elapsed, close_started = self._assert_bounded_stop(close)
        try:
            self.assertTrue(close_started.wait(timeout=0.1))
            self.assertIsNotNone(close.timeout)
            assert close.timeout is not None
            self.assertGreaterEqual(close.timeout, 0.0)
            self.assertLessEqual(close.timeout, 0.01)
            self.assertLess(elapsed, 0.15)
        finally:
            close.release.set()

    def test_legacy_close_signature_cannot_escape_stop_budget(self) -> None:
        class LegacySlowClose:
            def __init__(self) -> None:
                self.started = threading.Event()
                self.release = threading.Event()

            def __call__(self) -> None:
                self.started.set()
                self.release.wait(timeout=1)

        close = LegacySlowClose()
        elapsed, close_started = self._assert_bounded_stop(close)
        try:
            self.assertTrue(close_started.wait(timeout=0.1))
            self.assertLess(elapsed, 0.15)
        finally:
            close.release.set()

    def test_close_exception_does_not_skip_thread_join(self) -> None:
        client = OopzClient()
        close = Mock(side_effect=RuntimeError("close failed"))
        client._ws = cast(Any, SimpleNamespace(close=close))
        worker_done = threading.Event()
        client._thread = threading.Thread(
            target=lambda: (time.sleep(0.01), worker_done.set()),
            name="OopzClient",
            daemon=True,
        )
        client._thread.start()

        client.stop(timeout=0.2)

        self.assertTrue(worker_done.is_set())
        self.assertFalse(client._thread.is_alive())
        close.assert_called_once()


class CredentialRefreshTest(unittest.TestCase):
    def test_expired_jwt_triggers_refresh(self) -> None:
        refresher = Mock(return_value={
            "person_uid": "new-person",
            "device_id": "new-device",
            "jwt_token": _fake_jwt({"exp": int(time.time()) + 3600}),
        })
        client = OopzClient(credential_refresher=refresher)
        client._jwt_token = _fake_jwt({"exp": int(time.time()) - 10})

        self.assertTrue(client._maybe_refresh_expired_credentials())
        refresher.assert_called_once()
        self.assertEqual(client._person_id, "new-person")
        self.assertEqual(client._device_id, "new-device")

    def test_valid_jwt_does_not_trigger_refresh(self) -> None:
        refresher = Mock()
        client = OopzClient(credential_refresher=refresher)
        client._jwt_token = _fake_jwt({"exp": int(time.time()) + 3600})

        self.assertFalse(client._maybe_refresh_expired_credentials())
        refresher.assert_not_called()

    def test_refresh_is_throttled(self) -> None:
        refresher = Mock(return_value=None)
        client = OopzClient(
            credential_refresher=refresher,
            min_credential_refresh_interval=300.0,
        )
        client._jwt_token = _fake_jwt({"exp": int(time.time()) - 10})

        client._maybe_refresh_expired_credentials()
        client._maybe_refresh_expired_credentials()

        refresher.assert_called_once()

    def test_unparseable_token_does_not_refresh(self) -> None:
        refresher = Mock()
        client = OopzClient(credential_refresher=refresher)
        client._jwt_token = "opaque-token"

        self.assertFalse(client._maybe_refresh_expired_credentials())
        refresher.assert_not_called()


class AuthResponseHandlingTest(unittest.TestCase):
    def test_auth_failure_refreshes_and_reconnects(self) -> None:
        refresher = Mock(return_value={
            "person_uid": "p2",
            "device_id": "d2",
            "jwt_token": "t2",
        })
        client = OopzClient(credential_refresher=refresher)
        client._last_refresh_attempt = 0.0
        ws = _fake_ws()

        client._handle_auth_response(ws, {"body": json.dumps({"code": 401, "error": "expired"})})

        refresher.assert_called_once()
        ws.close.assert_called_once()
        self.assertEqual(client._jwt_token, "t2")

    def test_auth_success_keeps_connection(self) -> None:
        refresher = Mock()
        client = OopzClient(credential_refresher=refresher)
        ws = _fake_ws()

        client._handle_auth_response(ws, {"body": json.dumps({"code": "success"})})

        refresher.assert_not_called()
        ws.close.assert_not_called()


class ReconnectBackoffTest(unittest.TestCase):
    def test_short_session_keeps_backoff(self) -> None:
        client = OopzClient()
        client._consecutive_failures = 5
        client._session_started_at = time.time() - 3

        client._reset_backoff_if_session_healthy()

        self.assertEqual(client._consecutive_failures, 5)

    def test_long_session_resets_backoff(self) -> None:
        client = OopzClient()
        client._consecutive_failures = 5
        client._session_started_at = time.time() - 120

        client._reset_backoff_if_session_healthy()

        self.assertEqual(client._consecutive_failures, 0)


if __name__ == "__main__":
    unittest.main()
