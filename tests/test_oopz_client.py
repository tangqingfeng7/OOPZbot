import base64
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from oopz.oopz_client import (
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
        self.assertLess(result, 0)

    def test_valid_token_returns_positive(self) -> None:
        token = _fake_jwt({"exp": int(time.time()) + 3600})
        result = _jwt_expires_in(token)
        self.assertIsNotNone(result)
        self.assertGreater(result, 3000)

    def test_token_without_exp_returns_none(self) -> None:
        self.assertIsNone(_jwt_expires_in(_fake_jwt({"sub": "u1"})))

    def test_garbage_token_returns_none(self) -> None:
        self.assertIsNone(_jwt_expires_in("not-a-jwt"))
        self.assertIsNone(_jwt_expires_in(""))
        self.assertIsNone(_jwt_expires_in(None))


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
        self.assertFalse(_auth_response_failed("not a dict"))


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

        loop = threading.Thread(target=client._heartbeat_loop, args=(ws,), daemon=True)
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

        loop = threading.Thread(target=client._heartbeat_loop, args=(ws,), daemon=True)
        loop.start()
        time.sleep(0.1)
        client._running = False
        loop.join(timeout=2)

        ws.close.assert_not_called()


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
