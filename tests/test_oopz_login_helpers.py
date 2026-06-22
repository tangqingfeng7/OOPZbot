import base64
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


import oopz.oopz_password_login as password_login


PRIVATE_KEY_PEM = "-----BEGIN PRIVATE KEY-----\nunit-test-key\n-----END PRIVATE KEY-----"


class _Response:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


def _jwt_with_payload(payload: dict) -> str:
    header = {"alg": "RS256", "typ": "JWT"}

    def _part(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{_part(header)}.{_part(payload)}.signature"


class OopzPasswordLoginHelpersTest(unittest.TestCase):
    def test_replace_config_value_updates_only_requested_field(self) -> None:
        content = 'OOPZ_CONFIG = {"device_id": "old-device", "jwt_token": "old-token"}'

        updated, replaced = password_login._replace_config_value(content, "jwt_token", "new-token")

        self.assertTrue(replaced)
        self.assertIn('"device_id": "old-device"', updated)
        self.assertIn('"jwt_token": "new-token"', updated)

    def test_sanitize_credentials_masks_secrets_and_reports_jwt_expiry(self) -> None:
        token = _jwt_with_payload({"exp": int(time.time()) + 3600})
        credentials = {
            "person_uid": "1234567890",
            "device_id": "device-abcdef",
            "jwt_token": token,
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nxxx\n-----END PRIVATE KEY-----",
            "app_version": "69514",
        }

        sanitized = password_login._sanitize_credentials(credentials)

        self.assertEqual(sanitized["person_uid"], "1234***7890")
        self.assertEqual(sanitized["device_id"], "devi***cdef")
        self.assertNotEqual(sanitized["jwt_token"], token)
        self.assertTrue(sanitized["private_key"])
        self.assertFalse(sanitized["expired"])
        self.assertGreater(sanitized["expires_in_seconds"], 0)

    def test_save_credentials_writes_config_and_private_key_files(self) -> None:
        credentials = {
            "app_version": "70000",
            "device_id": "device-new",
            "person_uid": "person-new",
            "jwt_token": "jwt-new",
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.py"
            private_key_path = root / "private_key.py"
            config_path.write_text(
                (
                    'OOPZ_CONFIG = {\n'
                    '    "app_version": "old",\n'
                    '    "device_id": "old",\n'
                    '    "person_uid": "old",\n'
                    '    "jwt_token": "old",\n'
                    '}\n'
                ),
                encoding="utf-8",
            )

            with (
                patch.object(password_login, "CONFIG_PATH", str(config_path)),
                patch.object(password_login, "PRIVATE_KEY_PATH", str(private_key_path)),
                patch.object(password_login, "_apply_config_to_runtime"),
            ):
                saved = password_login.save_credentials(credentials)

            self.assertEqual(saved, ["config.py", "private_key.py"])
            config_text = config_path.read_text(encoding="utf-8")
            self.assertIn('"app_version": "70000"', config_text)
            self.assertIn('"device_id": "device-new"', config_text)
            self.assertIn('"person_uid": "person-new"', config_text)
            self.assertIn('"jwt_token": "jwt-new"', config_text)
            self.assertIn("PRIVATE_KEY_PEM", private_key_path.read_text(encoding="utf-8"))

    def test_builtin_login_bundle_restores_expected_material_shapes(self) -> None:
        signing_key = password_login.get_client_signing_key()
        password_modulus = password_login.get_client_password_modulus()

        self.assertIn("BEGIN PRIVATE KEY", signing_key)
        self.assertIn("END PRIVATE KEY", signing_key)
        self.assertGreater(len(password_modulus), 100)

    def test_builtin_login_bundle_rejects_tampering(self) -> None:
        bundle = {
            "salt": password_login._CLIENT_SIGNING_KEY_DATA["salt"],
            "chunks": list(password_login._CLIENT_SIGNING_KEY_DATA["chunks"]),
        }
        bundle["chunks"][0] = "A" + bundle["chunks"][0][1:]

        with self.assertRaises(password_login.OopzPasswordLoginError):
            password_login._restore_builtin_value(bundle, "signing")


class OopzApiPasswordLoginTest(unittest.TestCase):
    def test_build_password_login_body_uses_encrypted_password(self) -> None:
        with patch.object(password_login, "_encrypt_password_code", return_value="encrypted-code"):
            body = password_login._build_password_login_body(
                phone="13800138000",
                password="plain-password",
                device_id="device-1",
                public_n="public-modulus",
            )

        payload = json.loads(body)
        self.assertEqual(payload["loginType"], "PASSWORD")
        self.assertEqual(payload["phone"], "13800138000")
        self.assertEqual(payload["deviceId"], "device-1")
        self.assertEqual(payload["code"], "encrypted-code")
        self.assertNotIn("plain-password", body)

    def test_build_password_login_headers_contains_oopz_signing_fields(self) -> None:
        with patch.object(password_login, "_build_oopz_sign", return_value="signed-value"):
            headers = password_login._build_password_login_headers(
                device_id="device-1",
                body="{}",
                private_key_pem=PRIVATE_KEY_PEM,
            )

        self.assertEqual(headers["Oopz-Sign"], "signed-value")
        self.assertEqual(headers["Oopz-App-Version-Number"], "73817")
        self.assertEqual(headers["Oopz-Device-Id"], "device-1")
        self.assertIn("Oopz-Time", headers)
        self.assertIn("Oopz-Request-Id", headers)

    def test_login_with_api_password_returns_project_credentials(self) -> None:
        response = _Response(
            200,
            {
                "status": True,
                "data": {"uid": "person-1", "signature": "jwt-1"},
            },
        )

        with (
            patch.object(password_login, "_resolve_login_device_id", return_value="device-1"),
            patch.object(password_login, "get_client_signing_key", return_value=PRIVATE_KEY_PEM),
            patch.object(password_login, "get_client_password_modulus", return_value="public-modulus"),
            patch.object(password_login, "_encrypt_password_code", return_value="encrypted-code"),
            patch.object(password_login, "_build_oopz_sign", return_value="signed-value"),
            patch.object(password_login.requests, "post", return_value=response) as post,
        ):
            credentials = password_login.login_with_api_password(
                "13800138000",
                "plain-password",
                timeout=3,
            )

        self.assertEqual(credentials["person_uid"], "person-1")
        self.assertEqual(credentials["device_id"], "device-1")
        self.assertEqual(credentials["jwt_token"], "jwt-1")
        self.assertEqual(credentials["private_key_pem"], PRIVATE_KEY_PEM)
        self.assertEqual(credentials["app_version"], "73817")

        sent_body = post.call_args.kwargs["data"].decode("utf-8")
        sent_headers = post.call_args.kwargs["headers"]
        self.assertIn('"loginType":"PASSWORD"', sent_body)
        self.assertNotIn("plain-password", sent_body)
        self.assertEqual(sent_headers["Oopz-Sign"], "signed-value")
        self.assertEqual(post.call_args.kwargs["timeout"], 3)

    def test_login_with_api_password_reports_readable_failure(self) -> None:
        response = _Response(
            200,
            {
                "status": False,
                "data": {"msg": "密码错误"},
            },
        )

        with (
            patch.object(password_login, "_resolve_login_device_id", return_value="device-1"),
            patch.object(password_login, "get_client_signing_key", return_value=PRIVATE_KEY_PEM),
            patch.object(password_login, "get_client_password_modulus", return_value="public-modulus"),
            patch.object(password_login, "_encrypt_password_code", return_value="encrypted-code"),
            patch.object(password_login, "_build_oopz_sign", return_value="signed-value"),
            patch.object(password_login.requests, "post", return_value=response),
        ):
            with self.assertRaises(password_login.OopzPasswordLoginError) as ctx:
                password_login.login_with_api_password("13800138000", "bad-password")

        self.assertIn("密码错误", str(ctx.exception))

    def test_refresh_credentials_from_config_password_uses_config_login(self) -> None:
        config = types.ModuleType("config")
        config.OOPZ_CONFIG = {
            "login_phone": "13800138000",
            "login_password": "plain-password",
        }
        credentials = {
            "person_uid": "person-1",
            "device_id": "device-1",
            "jwt_token": "jwt-1",
            "private_key_pem": PRIVATE_KEY_PEM,
            "app_version": "73817",
        }

        with (
            patch.dict(sys.modules, {"config": config}),
            patch.object(password_login, "login_with_api_password", return_value=credentials) as api_login,
            patch.object(password_login, "save_credentials") as save_credentials,
        ):
            result = password_login.refresh_credentials_from_config_password(timeout=5)

        self.assertIs(result, credentials)
        api_login.assert_called_once_with("13800138000", "plain-password", timeout=5)
        save_credentials.assert_called_once_with(credentials)

    def test_refresh_credentials_from_config_password_skips_when_missing_config_login(self) -> None:
        config = types.ModuleType("config")
        config.OOPZ_CONFIG = {}

        with (
            patch.dict(sys.modules, {"config": config}),
            patch.object(password_login, "login_with_api_password") as api_login,
        ):
            result = password_login.refresh_credentials_from_config_password()

        self.assertIsNone(result)
        api_login.assert_not_called()

    def test_config_login_account_accepts_legacy_phone_password_fields(self) -> None:
        with patch.dict(os.environ, {"OOPZ_PHONE": "", "OOPZ_PASSWORD": ""}):
            phone, password = password_login._config_login_account(
                {"phone": "13800138000", "password": "plain-password"}
            )

        self.assertEqual(phone, "13800138000")
        self.assertEqual(password, "plain-password")


class OopzPasswordLoginFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_login_with_password_falls_back_to_browser_when_api_is_retryable(self) -> None:
        browser_result = {"ok": True, "saved": [], "credentials": {}, "raw": {}}

        with (
            patch.object(
                password_login,
                "login_with_api_password",
                side_effect=password_login.OopzPasswordLoginError("OOPZ 登录请求失败: timeout"),
            ),
            patch.object(
                password_login,
                "login_with_playwright_password",
                new=AsyncMock(return_value=browser_result),
            ) as browser_login,
        ):
            result = await password_login.login_with_password("13800138000", "pw", save=False)

        self.assertIs(result, browser_result)
        browser_login.assert_awaited_once()

    async def test_login_with_password_does_not_fallback_for_bad_password(self) -> None:
        browser_login = AsyncMock(return_value={"ok": True})

        with (
            patch.object(
                password_login,
                "login_with_api_password",
                side_effect=password_login.OopzPasswordLoginError("密码错误"),
            ),
            patch.object(password_login, "login_with_playwright_password", new=browser_login),
        ):
            with self.assertRaises(password_login.OopzPasswordLoginError):
                await password_login.login_with_password("13800138000", "bad-password", save=False)

        browser_login.assert_not_called()


class OopzClientCredentialsTest(unittest.TestCase):
    def test_update_credentials_refreshes_identity_and_closes_socket(self) -> None:
        config = types.ModuleType("config")
        config.OOPZ_CONFIG = {
            "person_uid": "old-person",
            "device_id": "old-device",
            "jwt_token": "old-token",
        }
        config.DEFAULT_HEADERS = {
            "User-Agent": "ua",
            "Origin": "https://web.oopz.cn",
            "Cache-Control": "no-cache",
            "Accept-Language": "zh-CN",
            "Accept-Encoding": "gzip",
        }
        name_resolver = types.ModuleType("oopz.name_resolver")
        name_resolver.get_resolver = lambda: None
        proxy_utils = types.ModuleType("core.proxy_utils")
        proxy_utils.get_websocket_proxy_kwargs = lambda proxy: {}
        websocket = types.ModuleType("websocket")

        sys.modules.pop("oopz.oopz_client", None)
        fake_modules = {
            "config": config,
            "oopz.name_resolver": name_resolver,
            "core.proxy_utils": proxy_utils,
            "websocket": websocket,
        }

        with patch.dict(sys.modules, fake_modules):
            import oopz.oopz_client as oopz_client

            class _Socket:
                closed = False

                def close(self):
                    self.closed = True

            client = oopz_client.OopzClient.__new__(oopz_client.OopzClient)
            client._person_id = "old-person"
            client._device_id = "old-device"
            client._jwt_token = "old-token"
            client._hb_body = json.dumps({"person": "old-person"})
            client._ws = _Socket()

            client.update_credentials("new-person", "new-device", "new-token")

            self.assertEqual(client._person_id, "new-person")
            self.assertEqual(client._device_id, "new-device")
            self.assertEqual(client._jwt_token, "new-token")
            self.assertEqual(json.loads(client._hb_body), {"person": "new-person"})
            self.assertTrue(client._ws.closed)

        sys.modules.pop("oopz_client", None)


class _SenderResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.content = b""
        self.headers = {}


class _SenderSession:
    def __init__(self, responses):
        self.headers = {}
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({
            "method": "GET",
            "url": url,
            "headers": headers or {},
            "params": params,
            "timeout": timeout,
        })
        return self._responses.pop(0)


class _SenderSigner:
    def __init__(self):
        self.private_key = "old-private-key"

    def oopz_headers(self, url_path: str, body_str: str):
        return {
            "X-Signer-Key": self.private_key,
            "X-Sign-Path": url_path,
            "X-Sign-Body": body_str,
        }


class OopzSenderAuthRefreshTest(unittest.TestCase):
    def test_get_refreshes_credentials_once_after_428_and_retries(self) -> None:
        from oopz.oopz_sender import OopzSender

        sender = OopzSender.__new__(OopzSender)
        sender.signer = _SenderSigner()
        sender.session = _SenderSession([_SenderResponse(428), _SenderResponse(200)])
        sender._area_members_cache = {"stale": {"data": {}}}
        sender._rate_lock = threading.Lock()
        sender._auth_refresh_lock = threading.Lock()
        sender._last_request_time = 0.0
        sender._RATE_LIMIT_INTERVAL = 0.0

        credentials = {
            "person_uid": "person-new",
            "device_id": "device-new",
            "jwt_token": "jwt-new",
            "private_key_pem": PRIVATE_KEY_PEM,
            "app_version": "73817",
        }

        with (
            patch(
                "oopz.oopz_password_login.refresh_credentials_from_config_password",
                return_value=credentials,
            ) as refresh,
            patch(
                "oopz.oopz_password_login.load_private_key_from_pem",
                return_value="new-private-key",
            ),
        ):
            response = sender._get("/userSubscribeArea/v1/list")

        self.assertEqual(response.status_code, 200)
        refresh.assert_called_once_with(timeout=20, save=True)
        self.assertEqual(len(sender.session.calls), 2)
        self.assertEqual(sender.session.calls[0]["headers"]["X-Signer-Key"], "old-private-key")
        self.assertEqual(sender.session.calls[1]["headers"]["X-Signer-Key"], "new-private-key")
        self.assertEqual(sender._area_members_cache, {})


if __name__ == "__main__":
    unittest.main()
