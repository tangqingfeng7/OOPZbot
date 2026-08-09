import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

try:
    import requests
    _REQUESTS_ERROR = None
except Exception as exc:
    _REQUESTS_ERROR = exc


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class NeteaseCookiePlaybackTest(unittest.TestCase):
    def setUp(self) -> None:
        if _REQUESTS_ERROR is not None:
            self.skipTest(f"缺少 requests 依赖: {_REQUESTS_ERROR}")
        self._old_config_module = sys.modules.get("config")
        self._old_netease_module = sys.modules.get("music.netease")
        fake_config = types.ModuleType("config")
        fake_config.__dict__["NETEASE_CLOUD"] = {
            "base_url": "http://netease.example",
            "cookie": "MUSIC_U=abc",
            "audio_quality": "exhigh",
        }
        sys.modules["config"] = fake_config
        sys.modules.pop("music.netease", None)
        import music.netease as netease

        self.module = netease

    def tearDown(self) -> None:
        sys.modules.pop("music.netease", None)
        if self._old_netease_module is not None:
            sys.modules["music.netease"] = self._old_netease_module
        if self._old_config_module is not None:
            sys.modules["config"] = self._old_config_module
        else:
            sys.modules.pop("config", None)

    def _client(self, session):
        client = self.module.NeteaseCloud.__new__(self.module.NeteaseCloud)
        client.base_url = "http://netease.example"
        client.cookie = "MUSIC_U=abc"
        client._session = session
        client._last_song_url_error = ""
        return client

    def test_get_song_url_posts_cookie_in_body_before_get(self) -> None:
        calls = []

        class FakeSession:
            def post(self, url, data=None, headers=None, timeout=10):
                calls.append(("POST", url, data or {}, headers or {}))
                return _FakeResponse({
                    "code": 200,
                    "data": [{
                        "id": 1,
                        "url": "https://music.example/full.mp3",
                        "time": 222000,
                        "size": 8_000_000,
                    }],
                })

            def get(self, url, params=None, headers=None, timeout=10):
                calls.append(("GET", url, params or {}, headers or {}))
                return _FakeResponse({"code": 500, "data": []})

        client = self._client(FakeSession())

        url = client.get_song_url(1, expected_duration_ms=222000, song_name="稻香")

        self.assertEqual(url, "https://music.example/full.mp3")
        self.assertEqual(len(calls), 1)
        method, request_url, data, headers = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(request_url, "http://netease.example/song/url/v1")
        self.assertEqual(data["id"], 1)
        self.assertEqual(data["level"], "exhigh")
        self.assertEqual(data["cookie"], "MUSIC_U=abc")
        self.assertEqual(headers["Cookie"], "MUSIC_U=abc")

    def test_get_user_id_prefers_nested_login_status_profile(self) -> None:
        class FakeSession:
            def __init__(self):
                self.calls = []

            def post(self, url, data=None, headers=None, timeout=10):
                self.calls.append(("POST", url, data or {}, headers or {}))
                return _FakeResponse({
                    "data": {
                        "code": 200,
                        "profile": {"userId": 399919346, "nickname": "测试账号"},
                    },
                })

            def get(self, url, params=None, headers=None, timeout=10):
                self.calls.append(("GET", url, params or {}, headers or {}))
                return _FakeResponse({"code": 200, "account": None, "profile": None})

        session = FakeSession()
        client = self._client(session)

        user_id = client.get_user_id()

        self.assertEqual(user_id, 399919346)
        self.assertEqual(len(session.calls), 1)
        method, request_url, data, headers = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(request_url, "http://netease.example/login/status")
        self.assertEqual(data["cookie"], "MUSIC_U=abc")
        self.assertIn("timestamp", data)
        self.assertEqual(headers["Cookie"], "MUSIC_U=abc")

    def test_get_user_id_does_not_fallback_to_another_endpoint(self) -> None:
        class FakeSession:
            def __init__(self):
                self.calls = []

            def post(self, url, data=None, headers=None, timeout=10):
                self.calls.append(("POST", url))
                return _FakeResponse({"data": {"code": 200, "profile": None}})

            def get(self, url, params=None, headers=None, timeout=10):
                self.calls.append(("GET", url))
                return _FakeResponse({
                    "code": 200,
                    "account": {"id": 123456, "userName": "旧版账号"},
                    "profile": None,
                })

        session = FakeSession()
        client = self._client(session)

        user_id = client.get_user_id()

        self.assertIsNone(user_id)
        self.assertEqual(
            session.calls,
            [("POST", "http://netease.example/login/status")],
        )

    def test_get_liked_ids_posts_cookie_in_body(self) -> None:
        class FakeSession:
            def __init__(self):
                self.calls = []

            def post(self, url, data=None, headers=None, timeout=10):
                self.calls.append(("POST", url, data or {}, headers or {}))
                return _FakeResponse({"code": 200, "ids": [10, 20, 30]})

            def get(self, url, params=None, headers=None, timeout=10):
                self.calls.append(("GET", url, params or {}, headers or {}))
                return _FakeResponse({"code": 200, "ids": []})

        session = FakeSession()
        client = self._client(session)

        liked_ids = client.get_liked_ids(399919346)

        self.assertEqual(liked_ids, [10, 20, 30])
        self.assertEqual(len(session.calls), 1)
        method, request_url, data, headers = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(request_url, "http://netease.example/likelist")
        self.assertEqual(data["uid"], 399919346)
        self.assertEqual(data["cookie"], "MUSIC_U=abc")
        self.assertIn("timestamp", data)
        self.assertEqual(headers["Cookie"], "MUSIC_U=abc")

    def test_get_liked_ids_does_not_fallback_after_failed_post(self) -> None:
        class FakeSession:
            def __init__(self):
                self.calls = []

            def post(self, url, data=None, headers=None, timeout=10):
                self.calls.append(("POST", url))
                return _FakeResponse({"code": 500})

            def get(self, url, params=None, headers=None, timeout=10):
                self.calls.append(("GET", url))
                return _FakeResponse({"code": 200, "ids": [40]})

        session = FakeSession()
        client = self._client(session)

        liked_ids = client.get_liked_ids(123456)

        self.assertEqual(liked_ids, [])
        self.assertEqual(
            session.calls,
            [("POST", "http://netease.example/likelist")],
        )

    def test_get_song_url_rejects_free_trial_audio(self) -> None:
        class FakeSession:
            def __init__(self):
                self.calls = []

            def post(self, url, data=None, headers=None, timeout=10):
                self.calls.append(("POST", url, data or {}))
                return _FakeResponse({
                    "code": 200,
                    "data": [{
                        "id": 2,
                        "url": "https://music.example/trial.mp3",
                        "time": 30040,
                        "size": 600_000,
                        "freeTrialInfo": {"start": 0, "end": 30},
                    }],
                })

            def get(self, url, params=None, headers=None, timeout=10):
                self.calls.append(("GET", url, params or {}))
                return _FakeResponse({"code": 200, "data": [{"id": 2, "url": None}]})

        session = FakeSession()
        client = self._client(session)

        url = client.get_song_url(2, expected_duration_ms=240000, song_name="会员歌")

        self.assertIsNone(url)
        self.assertIn("试听音频", client.last_song_url_error)
        self.assertGreaterEqual(len(session.calls), 2)

    def test_get_song_url_rejects_mismatched_response_id(self) -> None:
        """上游 NeteaseCloudMusicApi 偶发的缓存错乱：请求 id=A 返回 id=B 的 URL。
        必须在客户端拦截，否则会播错歌（UI 显示 A、实际播 B）。"""
        class FakeSession:
            def __init__(self):
                self.calls = []
                # 第一次 POST → 返回错乱响应 (id=999 不是请求的 1318235595)
                # 第二次 GET → 同样错乱
                # 第三次 GET (/song/url) → 返回正确的 id 用于验证 fallback
                self.post_count = 0

            def post(self, url, data=None, headers=None, timeout=10):
                self.calls.append(("POST", url, data or {}))
                self.post_count += 1
                return _FakeResponse({
                    "code": 200,
                    "data": [{
                        "id": 999,  # 错乱：跟请求的 1318235595 不一致
                        "url": "https://music.example/wrong.mp3",
                        "time": 269828,
                        "size": 10_808_214,
                    }],
                })

            def get(self, url, params=None, headers=None, timeout=10):
                self.calls.append(("GET", url, params or {}))
                # 第一次 GET 命中 /song/url/v1 (cookie 路径已被尝试)
                # /song/url fallback 也返回错乱
                return _FakeResponse({
                    "code": 200,
                    "data": [{
                        "id": 999,
                        "url": "https://music.example/wrong.mp3",
                        "time": 269828,
                        "size": 10_808_214,
                    }],
                })

        session = FakeSession()
        client = self._client(session)

        url = client.get_song_url(1318235595, expected_duration_ms=241266, song_name="耳朵")

        self.assertIsNone(url, "id 不匹配时必须丢弃错乱的 URL")
        self.assertIn("错乱", client.last_song_url_error)
        # 应该尝试了多次（v1 POST/GET + 兜底 path GET）
        self.assertGreaterEqual(len(session.calls), 2)

    def test_get_song_url_includes_timestamp_to_bust_cache(self) -> None:
        """请求里必须带 timestamp，破上游本地缓存防止拿到上一次的错乱响应。"""
        captured = []

        class FakeSession:
            def post(self, url, data=None, headers=None, timeout=10):
                captured.append(("POST", data or {}))
                return _FakeResponse({
                    "code": 200,
                    "data": [{
                        "id": 100,
                        "url": "https://music.example/ok.mp3",
                        "time": 222000,
                        "size": 8_000_000,
                    }],
                })

            def get(self, url, params=None, headers=None, timeout=10):
                captured.append(("GET", params or {}))
                return _FakeResponse({"code": 500, "data": []})

        client = self._client(FakeSession())
        url = client.get_song_url(100, expected_duration_ms=222000, song_name="A")

        self.assertEqual(url, "https://music.example/ok.mp3")
        self.assertGreaterEqual(len(captured), 1)
        first_method, first_params = captured[0]
        self.assertIn("timestamp", first_params, "请求必须带 timestamp 破上游缓存")
        self.assertGreater(int(first_params["timestamp"]), 0)


if __name__ == "__main__":
    unittest.main()
