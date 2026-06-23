import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    _TESTCLIENT_ERROR = None
except Exception as exc:  # pragma: no cover - 依赖缺失时跳过
    TestClient = None
    _TESTCLIENT_ERROR = exc


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


from domain.plugins.base import PluginCommandCapabilities, PluginDescriptor, PluginMetadata


class _FakePlugins:
    def __init__(self):
        self._descriptors = [
            PluginDescriptor(
                metadata=PluginMetadata(name="alpha", description="alpha desc", version="1.0.0"),
                capabilities=PluginCommandCapabilities(
                    mention_prefixes=("alpha",),
                    slash_commands=("alpha",),
                    is_public_command=True,
                ),
                builtin=False,
            )
        ]

    def discover(self):
        return ["alpha"]

    def list_descriptors(self):
        return list(self._descriptors)

    def enabled_plugin_names(self):
        return ["alpha"]

    def get_last_results(self):
        return {}

    @property
    def state_path(self):
        return "data/plugin_runtime_state.json"


class _FakeNeteaseResponse:
    def __init__(self, payload, headers=None, cookies=None):
        self._payload = payload
        self.headers = headers or {}
        self.cookies = cookies or []

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeRedisPipeline:
    def __init__(self, redis):
        self.redis = redis
        self.keys = []

    def get(self, key):
        self.keys.append(key)
        return self

    def execute(self):
        return [self.redis.get(key) for key in self.keys]


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, *args, **kwargs):
        self.store[key] = value
        return True

    def pipeline(self, transaction=False):
        return _FakeRedisPipeline(self)


class WebPlayerAdminTest(unittest.TestCase):
    def setUp(self) -> None:
        if TestClient is None:
            self.skipTest(f"缺少 TestClient 依赖: {_TESTCLIENT_ERROR}")
        import web.web_player as web_player

        self.module = web_player
        self.module.register_runtime_dependencies(
            music=SimpleNamespace(),
            plugins=_FakePlugins(),
            plugin_host=SimpleNamespace(),
        )
        self.client = TestClient(self.module.app)

    def test_plugins_api_requires_login(self) -> None:
        with patch.object(self.module, "_admin_enabled", return_value=True):
            response = self.client.get("/admin/api/plugins")

        self.assertEqual(response.status_code, 401)

    def test_plugins_api_returns_inventory_when_logged_in(self) -> None:
        with (
            patch.object(self.module, "_admin_enabled", return_value=True),
            patch.object(self.module, "_is_admin_authorized", return_value=True),
        ):
            response = self.client.get("/admin/api/plugins")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["enabled_plugins"], ["alpha"])
        self.assertEqual(data["plugins"][0]["name"], "alpha")

    def test_plugin_load_rejects_invalid_name(self) -> None:
        with (
            patch.object(self.module, "_admin_enabled", return_value=True),
            patch.object(self.module, "_is_admin_authorized", return_value=True),
        ):
            response = self.client.post("/admin/api/plugins/bad-name/load")

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data["ok"])
        self.assertIn("插件名不合法", data["error"])

    def test_member_endpoint_returns_503_when_sender_missing(self) -> None:
        import web.admin.shared._runtime as runtime

        with (
            patch.object(self.module, "_admin_enabled", return_value=True),
            patch.object(self.module, "_is_admin_authorized", return_value=True),
            patch.object(runtime, "_get_sender", return_value=None),
        ):
            response = self.client.get("/admin/api/members/blocks")

        self.assertEqual(response.status_code, 503)
        data = response.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "sender 未初始化")

    def test_player_page_does_not_pin_stale_active_area(self) -> None:
        r = _FakeRedis()
        r.set("music:web_access_token", "token-1")
        r.set("music:web_active_area", "old-area")

        with patch.object(self.module, "get_redis", return_value=r):
            response = self.client.get("/w/token-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn('window.__OOPZ_AREA__=""', response.text)
        self.assertNotIn("old-area", response.text)

    def test_player_page_can_pin_area_when_query_says_so(self) -> None:
        r = _FakeRedis()
        r.set("music:web_access_token", "token-1")
        r.set("music:web_active_area", "old-area")

        with patch.object(self.module, "get_redis", return_value=r):
            response = self.client.get("/w/token-1?area=area-2")

        self.assertEqual(response.status_code, 200)
        self.assertIn('window.__OOPZ_AREA__="area-2"', response.text)

    def test_player_cookie_is_not_secure_on_plain_http(self) -> None:
        r = _FakeRedis()
        r.set("music:web_access_token", "token-1")

        with (
            patch.object(self.module, "get_redis", return_value=r),
            patch.object(self.module.cfg, "cookie_secure", return_value=True),
        ):
            response = self.client.get("/w/token-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn("web_token=token-1", response.headers["set-cookie"])
        self.assertNotIn("Secure", response.headers["set-cookie"])

    def test_player_cookie_stays_secure_behind_https_proxy(self) -> None:
        r = _FakeRedis()
        r.set("music:web_access_token", "token-1")

        with (
            patch.object(self.module, "get_redis", return_value=r),
            patch.object(self.module.cfg, "cookie_secure", return_value=True),
        ):
            response = self.client.get("/w/token-1", headers={"x-forwarded-proto": "https"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("web_token=token-1", response.headers["set-cookie"])
        self.assertIn("Secure", response.headers["set-cookie"])

    def test_status_without_area_follows_active_area(self) -> None:
        r = _FakeRedis()
        r.set("music:web_access_token", "token-1")
        r.set("music:web_active_area", "area-2")
        r.set(
            self.module._area_key(self.module.KEY_CURRENT, "area-2"),
            json.dumps({"name": "稻香", "duration_ms": 222000}, ensure_ascii=False),
        )

        self.client.cookies.set("web_token", "token-1")
        with patch.object(self.module, "get_redis", return_value=r):
            response = self.client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["playing"])
        self.assertEqual(data["name"], "稻香")

    def test_control_without_area_follows_active_area(self) -> None:
        r = _FakeRedis()
        r.set("music:web_access_token", "token-1")
        r.set("music:web_active_area", "area-2")

        def fake_control(action, body, redis_client, area=""):
            return {"ok": True, "area": area, "action": action}

        with (
            patch.object(self.module, "get_redis", return_value=r),
            patch.object(self.module, "execute_control_action", side_effect=fake_control),
        ):
            self.client.cookies.set("web_token", "token-1")
            response = self.client.post("/api/control", json={"action": "volume", "volume": 50})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["area"], "area-2")

    def test_config_update_writes_config_py(self) -> None:
        import web.web_player_config as cfg

        baseline = copy.deepcopy(cfg.CONFIG_BASELINES["web_player"])
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.py"
            config_path.write_text(
                'WEB_PLAYER_CONFIG = {\n'
                '    "url": "",\n'
                '    "host": "0.0.0.0",\n'
                '}\n',
                encoding="utf-8",
            )
            try:
                with (
                    patch.object(self.module, "_admin_enabled", return_value=True),
                    patch.object(self.module, "_is_admin_authorized", return_value=True),
                    patch.object(cfg, "CONFIG_FILE_PATH", str(config_path)),
                ):
                    response = self.client.post(
                        "/admin/api/config",
                        json={
                            "updates": {"web_player": {"url": "https://example.test"}},
                            "persist": True,
                        },
                    )

                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertTrue(data["ok"])
                self.assertTrue(data["persisted"])
                self.assertEqual(data["config_source"], "config.py")
                self.assertIn('"url": "https://example.test"', config_path.read_text(encoding="utf-8"))
            finally:
                cfg.WEB_PLAYER_CONFIG.clear()
                cfg.WEB_PLAYER_CONFIG.update(copy.deepcopy(baseline))

    def test_oopz_login_payload_falls_back_to_config_account(self) -> None:
        import web.web_player_config as cfg
        from web.admin.config import _parse_oopz_login_payload

        with patch.object(
            cfg,
            "OOPZ_CONFIG",
            {"login_phone": "13800138000", "login_password": "plain-password"},
        ):
            phone, password, timeout = _parse_oopz_login_payload({})

        self.assertEqual(phone, "13800138000")
        self.assertEqual(password, "plain-password")
        self.assertEqual(timeout, 90.0)

    def test_netease_qr_login_returns_qr_image_when_logged_in(self) -> None:
        calls = []

        def fake_get(base_url, path, params=None, **_kwargs):
            calls.append((base_url, path, params or {}))
            if path == "/login/qr/key":
                payload = {"code": 200, "data": {"unikey": "qr-key"}}
                return payload, _FakeNeteaseResponse(payload)
            if path == "/login/qr/create":
                self.assertEqual((params or {}).get("key"), "qr-key")
                self.assertEqual((params or {}).get("qrimg"), "true")
                payload = {
                    "code": 200,
                    "data": {
                        "qrimg": "data:image/png;base64,abc",
                        "qrurl": "orpheus://qr",
                    },
                }
                return payload, _FakeNeteaseResponse(payload)
            raise AssertionError(f"unexpected path: {path}")

        with (
            patch.object(self.module, "_admin_enabled", return_value=True),
            patch.object(self.module, "_is_admin_authorized", return_value=True),
            patch("web.admin.config._netease_api_get", side_effect=fake_get),
        ):
            response = self.client.post(
                "/admin/api/netease/login/qr",
                json={"base_url": "http://localhost:3000/"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["key"], "qr-key")
        self.assertEqual(data["qrimg"], "data:image/png;base64,abc")
        self.assertEqual(calls[0][:2], ("http://localhost:3000", "/login/qr/key"))
        self.assertEqual(calls[1][:2], ("http://localhost:3000", "/login/qr/create"))

    def test_netease_qr_check_returns_cookie_and_profile_on_success(self) -> None:
        def fake_get(base_url, path, params=None, **_kwargs):
            self.assertEqual(base_url, "http://localhost:3000")
            self.assertEqual(path, "/login/qr/check")
            self.assertEqual((params or {}).get("key"), "qr-key")
            payload = {
                "code": 200,
                "data": {
                    "code": 803,
                    "message": "授权登录成功",
                    "cookie": "MUSIC_U=abc; __csrf=def",
                },
            }
            return payload, _FakeNeteaseResponse(payload)

        def fake_post(base_url, path, data=None, **kwargs):
            self.assertEqual(base_url, "http://localhost:3000")
            self.assertEqual(path, "/login/status")
            self.assertEqual((kwargs.get("headers") or {}).get("Cookie"), "MUSIC_U=abc; __csrf=def")
            self.assertEqual((data or {}).get("cookie"), "MUSIC_U=abc; __csrf=def")
            payload = {
                "code": 200,
                "data": {"profile": {"userId": 12345, "nickname": "测试账号"}},
            }
            return payload, _FakeNeteaseResponse(payload)

        with (
            patch.object(self.module, "_admin_enabled", return_value=True),
            patch.object(self.module, "_is_admin_authorized", return_value=True),
            patch("web.admin.config._netease_api_get", side_effect=fake_get),
            patch("web.admin.config._netease_account_status", return_value={
                "ok": True,
                "logged_in": True,
                "profile": {"user_id": "12345", "nickname": "测试账号"},
            }),
        ):
            response = self.client.post(
                "/admin/api/netease/login/qr/check",
                json={"base_url": "http://localhost:3000", "key": "qr-key"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["cookie"], "MUSIC_U=abc; __csrf=def")
        self.assertEqual(data["profile"]["user_id"], "12345")
        self.assertEqual(data["profile"]["nickname"], "测试账号")

    def test_netease_account_endpoint_returns_current_profile(self) -> None:
        def fake_post(base_url, path, data=None, **kwargs):
            self.assertEqual(base_url, "http://localhost:3000")
            self.assertEqual(path, "/login/status")
            self.assertEqual((kwargs.get("headers") or {}).get("Cookie"), "MUSIC_U=abc")
            self.assertEqual((data or {}).get("cookie"), "MUSIC_U=abc")
            payload = {
                "code": 200,
                "data": {"profile": {"userId": 67890, "nickname": "已保存账号"}},
            }
            return payload, _FakeNeteaseResponse(payload)

        with (
            patch.object(self.module, "_admin_enabled", return_value=True),
            patch.object(self.module, "_is_admin_authorized", return_value=True),
            patch("web.admin.config.cfg.NETEASE_CLOUD", {"base_url": "http://localhost:3000", "cookie": "MUSIC_U=abc"}),
            patch("web.admin.config._netease_account_status", return_value={
                "ok": True,
                "logged_in": True,
                "profile": {"user_id": "67890", "nickname": "已保存账号"},
            }),
        ):
            response = self.client.get("/admin/api/netease/account")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["logged_in"])
        self.assertEqual(data["profile"]["user_id"], "67890")
        self.assertEqual(data["profile"]["nickname"], "已保存账号")

    def test_bilibili_qr_login_returns_qr_image_when_logged_in(self) -> None:
        def fake_get(path, params=None):
            self.assertEqual(path, "/x/passport-login/web/qrcode/generate")
            payload = {
                "code": 0,
                "data": {
                    "qrcode_key": "bili-key",
                    "url": "https://passport.bilibili.com/h5-app/passport/login/scan?qrcode_key=bili-key",
                },
            }
            return payload, _FakeNeteaseResponse(payload)

        with (
            patch.object(self.module, "_admin_enabled", return_value=True),
            patch.object(self.module, "_is_admin_authorized", return_value=True),
            patch("web.admin.config._bilibili_api_get", side_effect=fake_get),
            patch("web.admin.config._make_qr_data_uri", return_value="data:image/png;base64,bili"),
        ):
            response = self.client.post("/admin/api/bilibili/login/qr", json={})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["key"], "bili-key")
        self.assertEqual(data["qrimg"], "data:image/png;base64,bili")

    def test_bilibili_qr_check_returns_cookie_and_profile_on_success(self) -> None:
        def fake_get(path, params=None):
            self.assertEqual(path, "/x/passport-login/web/qrcode/poll")
            self.assertEqual((params or {}).get("qrcode_key"), "bili-key")
            payload = {
                "code": 0,
                "data": {
                    "code": 0,
                    "message": "扫描登录成功",
                    "url": (
                        "https://passport.bilibili.com/crossDomain?"
                        "DedeUserID=100&DedeUserID__ckMd5=md5&SESSDATA=sess&"
                        "bili_jct=csrf&sid=abc"
                    ),
                },
            }
            return payload, _FakeNeteaseResponse(payload)

        with (
            patch.object(self.module, "_admin_enabled", return_value=True),
            patch.object(self.module, "_is_admin_authorized", return_value=True),
            patch("web.admin.config._bilibili_api_get", side_effect=fake_get),
            patch("web.admin.config._bilibili_account_status", return_value={
                "ok": True,
                "logged_in": True,
                "profile": {"user_id": "100", "nickname": "B站测试账号"},
            }),
        ):
            response = self.client.post(
                "/admin/api/bilibili/login/qr/check",
                json={"key": "bili-key"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "success")
        self.assertEqual(
            data["cookie"],
            "SESSDATA=sess; bili_jct=csrf; DedeUserID=100; DedeUserID__ckMd5=md5; sid=abc",
        )
        self.assertEqual(data["profile"]["user_id"], "100")
        self.assertEqual(data["profile"]["nickname"], "B站测试账号")

    def test_bilibili_account_endpoint_returns_current_profile(self) -> None:
        def fake_get(path, headers=None):
            self.assertEqual(path, "/x/web-interface/nav")
            self.assertEqual((headers or {}).get("Cookie"), "SESSDATA=sess")
            payload = {
                "code": 0,
                "message": "0",
                "data": {
                    "isLogin": True,
                    "mid": 24680,
                    "uname": "已保存B站账号",
                    "face": "https://i0.hdslb.com/face.jpg",
                },
            }
            return payload, _FakeNeteaseResponse(payload)

        with (
            patch.object(self.module, "_admin_enabled", return_value=True),
            patch.object(self.module, "_is_admin_authorized", return_value=True),
            patch("web.admin.config.cfg.BILIBILI_MUSIC_CONFIG", {"enabled": True, "cookie": "SESSDATA=sess"}),
            patch("web.admin.config._bilibili_account_status", return_value={
                "ok": True,
                "logged_in": True,
                "profile": {"user_id": "24680", "nickname": "已保存B站账号"},
            }),
        ):
            response = self.client.get("/admin/api/bilibili/account")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["logged_in"])
        self.assertEqual(data["profile"]["user_id"], "24680")
        self.assertEqual(data["profile"]["nickname"], "已保存B站账号")

    def test_admin_html_pages_reference_shared_shell_assets(self) -> None:
        paths = [
            "/admin",
            "/admin/music",
            "/admin/config",
            "/admin/stats",
            "/admin/system",
            "/admin/setup",
        ]

        with patch.object(self.module, "_admin_enabled", return_value=True):
            for path in paths:
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('/admin-assets/admin-shell.css', response.text)
                    self.assertIn('/admin-assets/admin-shell.js', response.text)
                    self.assertIn('class="shell-topbar"', response.text)
                    self.assertIn('id="topNav"', response.text)
                    self.assertIn('id="mobileNav"', response.text)
                    self.assertIn('id="topStatus"', response.text)

    def test_setup_diagnostics_api_returns_report_when_logged_in(self) -> None:
        fake_report = {
            "status": "warn",
            "summary": {"pass": 3, "warn": 1, "fail": 0, "info": 1},
            "checks": [{"id": "redis", "level": "pass", "title": "Redis 连接", "summary": "Redis 连接正常"}],
            "wizard_steps": [{"id": "runtime", "status": "done", "title": "打通基础运行时"}],
            "first_run_needed": True,
            "quick_links": [],
        }

        with (
            patch.object(self.module, "_admin_enabled", return_value=True),
            patch.object(self.module, "_is_admin_authorized", return_value=True),
            patch("web.admin.music.SetupDiagnostics") as diagnostics_cls,
        ):
            diagnostics_cls.return_value.build_report.return_value = fake_report
            response = self.client.get("/admin/api/setup/diagnostics")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "warn")
        self.assertEqual(data["summary"]["warn"], 1)
        self.assertEqual(data["checks"][0]["title"], "Redis 连接")

    def test_scheduled_message_templates_api_returns_items_when_logged_in(self) -> None:
        with (
            patch.object(self.module, "_admin_enabled", return_value=True),
            patch.object(self.module, "_is_admin_authorized", return_value=True),
        ):
            response = self.client.get("/admin/api/scheduled-message-templates")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(len(data["items"]) >= 1)
        self.assertIn("key", data["items"][0])

    def test_scheduled_message_template_apply_creates_task(self) -> None:
        with (
            patch.object(self.module, "_admin_enabled", return_value=True),
            patch.object(self.module, "_is_admin_authorized", return_value=True),
            patch("web.admin.scheduler.ScheduledMessageDB.create", return_value=99) as create_task,
        ):
            response = self.client.post(
                "/admin/api/scheduled-message-templates/morning/apply",
                json={"channel_id": "channel-1", "area_id": "area-1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], 99)
        create_task.assert_called_once()


if __name__ == "__main__":
    unittest.main()
