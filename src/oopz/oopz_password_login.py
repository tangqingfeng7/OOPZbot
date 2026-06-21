"""后台 OOPZ 账号密码登录与凭据落盘。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import os
import re
import time
import uuid
import zlib
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from core.logger_config import get_logger

logger = get_logger("OopzPasswordLogin")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.py")
CONFIG_EXAMPLE_PATH = os.path.join(PROJECT_ROOT, "config.example.py")
PRIVATE_KEY_PATH = os.path.join(PROJECT_ROOT, "private_key.py")
BROWSER_DATA_DIR = os.path.join(PROJECT_ROOT, "data", "oopz_admin_login_profile")
CHROMIUM_RUNTIME_DIR = os.path.join(PROJECT_ROOT, "data", "chromium_runtime")
OOPZ_WEB_URL = "https://web.oopz.cn/#/login"
LOGIN_RESPONSE_PATH = "/client/v1/login/v2/login"
LOGIN_API_URL = "https://gateway.oopz.cn" + LOGIN_RESPONSE_PATH
CLIENT_VERSION = "0.73.817"
APP_VERSION_NUMBER = "73817"
OOPZ_PLATFORM = "windows"
OOPZ_CHANNEL = "Web"
PUBLIC_E = "AQAB"
WS_EVENT_AUTH = 253
OOPZ_CONFIG_CREDENTIAL_FIELDS = ("app_version", "device_id", "person_uid", "jwt_token")
REQUIRED_CAPTURE_FIELDS = ("person_uid", "device_id", "jwt_token", "private_key_pem")
_CLIENT_SIGNING_KEY_DATA = {
    "salt": "oopz-login-sign-v2",
    "chunks": [
        "EyUuwmTGEeJbeNkxZehfuaIgd7YE9MHWWbQQeuyu3eE",
        "ioy4VMPfyzki7dfbBprgpkxbH6bEQRpPFiNog",
        "qEyhGhTKdcKsur7ajOzgm2kD9p9jOfrqe3bQi9-U8zf8XZrr1xa9urG4_uY",
        "oVG1ecUomzo5oPhE_AtLqLscdhvHFqQ",
        "PHVLGc-wkQ-8bLL7u1eN-K3PSxv5ovrzV5XnjF0QguiZneX",
        "JuahbpjKw-2W1THpbzDN7FiFRmcZbl7h8KoBzUUNoP7QQcGoqrq8F",
        "0S0n3TzPsw4eyrvs-dFc5f8OW3bjKb2RQbf1x5S9ZC8",
        "8xuGTG-UxDXcDgjxjoDqR2uU57_93_DDGa1mg",
        "-WiRh8DK2hIs7JmqQI1lsH3lbS4x9hw3PKFMmY_Xe-0-BCqzLyqUPbQcSOC",
        "suHnqHzHdCv1MFu-6g-zIcJEZ7HFYP9",
        "hNUIwAdtPLhUH-knkVi4PW41Pl0nuoFuugaNmfD3UUNWH7V",
        "BlQIWuClDCHvwCk-YKZTDlT6F7ihaFb50Y2Z8voYY8uyIN-5bvTr6",
        "r7FTo-nP0Vgrhoqinta5qbpJA8UAM_KNARHEGx4uA8J",
        "DrU9F-l8eibgxfygpyrvwX6ANoaTc0UZ7aefC",
        "V9y3Kvwsn8Hm10pHpPfzyrdNMQoAd3vTjvAQbfRDStjlL9w-5z5hm-OSg37",
        "X21Z8_httNQve8xgCCpUkG7Pe3ncVnR",
        "DBfqVmmud-UBr6FWm9y2gISNxW-8ywSY0_G2szpIpNtb3Ir",
        "V866RDdEKoST3WEyQUAhSmfLbARvksR7h7e0OTAmXmyVan26ZT-zS",
        "FEpZgFv0FF8tfKcrDtxkCJ-4WB_cEg_bzMyczH_VtcD",
        "fi3E30olZp7Dc2rSimgNWBwHsXZJJaTSPuIFC",
        "7ZGGAhRY5DeU82es60470wFl3sUoWOahT2aMEBBS_V_GAkt0rT-nOxjDi2Q",
        "7bqOIOAdy5Fv7VZdIUEeLY5UX8OGrlr",
        "LKfQ8OWZfgwYvoQJLYtOIMAxIhLkI75O_MSc9tJCN6Xu9sy",
        "oeXvK9jEX8vdw_yz7TISVzw-lNCIoXINDy3fAxq5_mMXlXzBZB2Db",
        "DVRNJPOLI8EBxOrGORnsgAYO-SN5ciu1tC_JaPflwij",
        "MlnGF85PoO5ib6TcYAP-QxhaiuJRoukQub8aF",
        "ypDDJHjA3mXlSCprfvpgzRr83icEr8v5qbZSc5gZRR7uea3UbUfrBK69YR1",
        "16lPf1HB9DARry1M9WXk3ahZ61jTnsy",
        "N9IQ5gXDkPdTuVckPT5bWnPeosMFlYwZSRj1YZ3wIVlGnFT",
        "0IdisLbeBo9KLbX72uYNqly3lH2bjdb7cf56qgwXBogrYS8nvA4Nr",
        "ZUsUev8D8Wzx-Vc0zPWSUmKcAQx2Jy3cr22uLqU04dN",
        "rBBERTNCRh21JB4CCy1THqu0TyH3gsusAq1jZ",
        "p4RWorzhXAH0UsZbfejzKATZxOyZq8Izv4Kg-58RqDSP4pyaoE0reahJKH3",
        "0xhL4Xk5hG1V7zHAlSDTXPRwJ9kFWw-",
        "rUoSh6niRDqN8YiCTgqoSDPAEeGJGAGAp6ngK91Ov_TduGU",
        "oH8i9eKW_iA5DHBduJhTijv1Fq0Jb9Pqeuumff_LApeF6X1lHEEqm",
        "C9IGQbkjBpqO7EwlzD1USp60ZH9tgbk9JyhtWvemkGa",
        "n5C5U6nDuUip2xcvviRKtcEipEQ1oEGhD6-J6",
        "-f5jkU-B2N_h3-Ba1WS3Tr-GefyHM8-vL2wbmXh8XpYlF_6yDOLX60rLq_2",
        "QHu-BSof7vXcQJqOcV3",
    ],
}

_CLIENT_PASSWORD_MODULUS_DATA = {
    "salt": "oopz-login-pass-v2",
    "chunks": [
        "t8TUm1XdqQ86p9BhsKCpD-ug5pMhBHyeQTPqqx6EL4v",
        "mJIpoNx0_v6bFaaJ6VB883HryJHQHXKRt7A6c",
        "s_c8-M0ab407dTEJHUnoQQbN57xTh3J_DgCCY0nxNAzU_srh8brFr0ONXr9",
        "XdmhJT9pT30dbcBONFS0VWGC2q_VP3M",
        "8qG6Nx4g3ySNaixtE5qyKXcEcT9Lie9qE5mFwPT9wuNMy2w",
        "NAwwzS0EkU8_U0SUwgBRy_ZLC1AEk4FvK5MD4P0k7-BjJ826Ehiv-",
        "PntTxWcLdWPHMdSSBvsua24gx_8AfJLjKtLAzcn-4O2",
        "EWPNNQkHB1Vv2sW--IHa-d5ZI5iblAmzSQORc",
        "He1Lzkc-hXsljr4MJdX1N4Zjw8YPz1oWA_ZVTg0MUFTjjXN_a9NYeU",
    ],
}

try:
    from music.voice_client import _BROWSER_ARGS as _VOICE_BROWSER_ARGS
except Exception:
    _VOICE_BROWSER_ARGS = []

# 复用语音推流的 Chromium 参数，但登录页需要遵循 OOPZ/系统代理设置。
_BROWSER_ARGS = [arg for arg in _VOICE_BROWSER_ARGS if arg != "--no-proxy-server"]
for _arg in (
    "--disable-blink-features=AutomationControlled",
    "--autoplay-policy=no-user-gesture-required",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-crash-reporter",
    "--disable-crashpad",
):
    if _arg not in _BROWSER_ARGS:
        _BROWSER_ARGS.append(_arg)


def _get_chromium_executable_path() -> Optional[str]:
    """读取容器或宿主机指定的 Chromium 可执行文件路径。"""
    path = os.environ.get("BOT_CHROMIUM_EXECUTABLE_PATH") or os.environ.get("CHROME_BIN")
    if not path:
        return None
    path = path.strip()
    if not path:
        return None
    if os.path.exists(path):
        return path
    logger.warning("指定的 Chromium 路径不存在，回退到 Playwright 默认浏览器: %s", path)
    return None


def _is_writable_dir(path: str) -> bool:
    try:
        return bool(path) and os.path.isdir(path) and os.access(path, os.W_OK)
    except Exception:
        return False


def _prepare_chromium_runtime() -> tuple[dict[str, str], str]:
    """给 Docker 中的 Chromium 准备可写 HOME/XDG/Crashpad 目录。"""
    home_dir = os.path.join(CHROMIUM_RUNTIME_DIR, "home")
    config_dir = os.path.join(CHROMIUM_RUNTIME_DIR, "config")
    cache_dir = os.path.join(CHROMIUM_RUNTIME_DIR, "cache")
    crash_dir = os.path.join(CHROMIUM_RUNTIME_DIR, "crashpad")
    for path in (home_dir, config_dir, cache_dir, crash_dir):
        os.makedirs(path, exist_ok=True)

    env = dict(os.environ)
    if not _is_writable_dir(env.get("HOME", "")):
        env["HOME"] = home_dir
    if not _is_writable_dir(env.get("XDG_CONFIG_HOME", "")):
        env["XDG_CONFIG_HOME"] = config_dir
    if not _is_writable_dir(env.get("XDG_CACHE_HOME", "")):
        env["XDG_CACHE_HOME"] = cache_dir
    return env, crash_dir


def _chromium_args(crash_dir: str) -> list[str]:
    args = list(_BROWSER_ARGS)
    crash_arg = f"--crash-dumps-dir={crash_dir}"
    if crash_arg not in args:
        args.append(crash_arg)
    return args


def _new_credentials() -> dict[str, Any]:
    """创建一次登录捕获所需的凭据容器。"""
    return {
        "person_uid": None,
        "device_id": None,
        "jwt_token": None,
        "private_key_pem": None,
        "app_version": None,
    }


def _missing_required_credentials(credentials: dict[str, Any]) -> list[str]:
    return [key for key in REQUIRED_CAPTURE_FIELDS if not credentials.get(key)]


class OopzPasswordLoginError(RuntimeError):
    """OOPZ 自动登录失败。"""

    def __init__(
        self,
        message: str,
        *,
        code: int | str | None = None,
        payload: object | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload


def _bundle_stream(length: int, salt: bytes, label: str) -> bytes:
    output = bytearray()
    counter = 0
    seed = hashlib.sha256(salt + b":" + label.encode("ascii")).digest()
    while len(output) < length:
        output.extend(hashlib.sha256(seed + counter.to_bytes(4, "big") + salt).digest())
        counter += 1
    return bytes(output[:length])


def _rotate_right(value: int, bits: int) -> int:
    return ((value >> bits) | (value << (8 - bits))) & 0xFF


def _restore_builtin_value(bundle: Mapping[str, Any], label: str) -> str:
    salt_text = str(bundle.get("salt") or "")
    chunks = bundle.get("chunks") or []
    if not salt_text or not isinstance(chunks, list):
        raise OopzPasswordLoginError("内置登录素材格式错误")

    encoded = "".join(str(chunk)[::-1] for chunk in chunks)
    encoded += "=" * ((4 - len(encoded) % 4) % 4)
    try:
        mixed = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except Exception as exc:
        raise OopzPasswordLoginError("内置登录素材解码失败") from exc

    salt = salt_text.encode("ascii")
    stream = _bundle_stream(len(mixed), salt, label)
    payload = bytearray()
    for index, byte in enumerate(mixed):
        shift = ((salt[index % len(salt)] + index) % 7) + 1
        payload.append(_rotate_right(byte, shift) ^ stream[index])

    if len(payload) <= 12:
        raise OopzPasswordLoginError("内置登录素材长度异常")
    checksum = bytes(payload[:12])
    compressed = bytes(payload[12:])
    try:
        raw = zlib.decompress(compressed)
    except Exception as exc:
        raise OopzPasswordLoginError("内置登录素材解压失败") from exc
    if hashlib.sha256(raw).digest()[:12] != checksum:
        raise OopzPasswordLoginError("内置登录素材校验失败")
    return raw.decode("utf-8")


def get_client_signing_key() -> str:
    return _restore_builtin_value(_CLIENT_SIGNING_KEY_DATA, "signing")


def get_client_password_modulus() -> str:
    return _restore_builtin_value(_CLIENT_PASSWORD_MODULUS_DATA, "password")


# 页面加载前注入：让 OOPZ Web 端生成/导入的签名私钥可导出。
JS_CRYPTO_HOOK = """
(() => {
    window.__oopz_captured_pem = null;
    window.__oopz_key_events = [];

    const _subtle = crypto.subtle;
    const _importKey   = _subtle.importKey.bind(_subtle);
    const _generateKey = _subtle.generateKey.bind(_subtle);
    const _sign        = _subtle.sign.bind(_subtle);
    const _exportKey   = _subtle.exportKey.bind(_subtle);

    async function exportAsPem(key) {
        try {
            const ab    = await _exportKey('pkcs8', key);
            const bytes = new Uint8Array(ab);
            let bin = '';
            for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
            const b64   = btoa(bin);
            const lines = b64.match(/.{1,64}/g) || [];
            return '-----BEGIN PRIVATE KEY-----\\n' + lines.join('\\n') + '\\n-----END PRIVATE KEY-----';
        } catch (e) {
            window.__oopz_key_events.push({action: 'export_failed', error: e.message});
            return null;
        }
    }

    crypto.subtle.importKey = async function(format, keyData, algorithm, extractable, keyUsages) {
        const isSignKey = keyUsages && keyUsages.includes('sign');
        if (isSignKey) extractable = true;

        const key = await _importKey(format, keyData, algorithm, extractable, keyUsages);

        if (key && key.type === 'private') {
            window.__oopz_key_events.push({action: 'importKey', format, extractable: key.extractable});
            if (!window.__oopz_captured_pem && key.extractable) {
                window.__oopz_captured_pem = await exportAsPem(key);
            }
        }
        return key;
    };

    crypto.subtle.generateKey = async function(algorithm, extractable, keyUsages) {
        const isSignKey = keyUsages && keyUsages.includes('sign');
        if (isSignKey) extractable = true;

        const result = await _generateKey(algorithm, extractable, keyUsages);
        const pk = result && result.privateKey ? result.privateKey
                 : (result && result.type === 'private') ? result : null;

        if (pk) {
            window.__oopz_key_events.push({action: 'generateKey', extractable: pk.extractable});
            if (!window.__oopz_captured_pem && pk.extractable) {
                window.__oopz_captured_pem = await exportAsPem(pk);
            }
        }
        return result;
    };

    crypto.subtle.sign = async function(algorithm, key, data) {
        if (key && key.type === 'private' && !window.__oopz_captured_pem) {
            window.__oopz_key_events.push({action: 'sign', extractable: key.extractable});
            if (key.extractable) {
                window.__oopz_captured_pem = await exportAsPem(key);
            }
        }
        return _sign(algorithm, key, data);
    };
})();
"""

JS_GET_CAPTURED = """
() => ({
    pem: window.__oopz_captured_pem || null,
    events: window.__oopz_key_events || [],
})
"""

JS_CLEAR_INDEXEDDB = """
async () => {
    const deleted = [];
    try {
        try { localStorage.clear(); } catch (e) {}
        try { sessionStorage.clear(); } catch (e) {}
        const dbs = await indexedDB.databases();
        for (const db of dbs) {
            if (!db.name) continue;
            await new Promise((resolve) => {
                const req = indexedDB.deleteDatabase(db.name);
                req.onsuccess = () => resolve();
                req.onerror = () => resolve();
                req.onblocked = () => resolve();
            });
            deleted.push(db.name);
        }
    } catch (e) {}
    return deleted;
}
"""


def _mask(value: Optional[str], keep: int = 4) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return text[:keep] + "***"
    return f"{text[:keep]}***{text[-keep:]}"


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode("utf-8")))
    except Exception:
        return {}


def _jwt_exp_info(token: str) -> dict[str, Any]:
    payload = _jwt_payload(token)
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return {"exp": None, "expires_at": "", "expires_in_seconds": None, "expired": False}
    now = time.time()
    return {
        "exp": int(exp),
        "expires_at": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
        "expires_in_seconds": max(0, int(exp - now)),
        "expired": exp <= now,
    }


def _extract_error_code(payload: Any) -> int | str | None:
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if code in (None, ""):
        data = payload.get("data")
        if isinstance(data, dict):
            code = data.get("code")
    if code in (None, ""):
        return None
    return code


def _safe_response_error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "登录接口返回异常"
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for source in (payload, data):
        for key in ("message", "msg", "error", "errorMessage", "reason"):
            value = source.get(key)
            if value:
                return str(value)
    code = _extract_error_code(payload)
    if code not in (None, ""):
        return f"登录失败，错误码：{code}"
    return "登录失败，请检查账号密码或风控验证"


def _compact_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _now_ms() -> str:
    return str(int(time.time() * 1000))


def _normalize_private_key(pem: str) -> str:
    pem = pem.strip()
    if (pem.startswith("'") and pem.endswith("'")) or (
        pem.startswith('"') and pem.endswith('"')
    ):
        pem = pem[1:-1]
    pem = pem.replace("\\r\\n", "\n")
    pem = pem.replace("\\n", "\n")
    pem = pem.replace("\r\n", "\n")
    return pem.strip()


def _load_signing_private_key(private_key_pem: str):
    private_key_pem = _normalize_private_key(private_key_pem)
    if not private_key_pem.startswith("-----BEGIN PRIVATE KEY-----"):
        raise OopzPasswordLoginError("内置登录签名私钥格式错误")
    if "-----END PRIVATE KEY-----" not in private_key_pem:
        raise OopzPasswordLoginError("内置登录签名私钥缺少 END PRIVATE KEY")
    try:
        return serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"),
            password=None,
        )
    except Exception as exc:
        raise OopzPasswordLoginError(f"无法加载内置登录签名私钥: {exc}") from exc


def _b64url_decode_int(value: str) -> int:
    value += "=" * ((4 - len(value) % 4) % 4)
    raw = base64.urlsafe_b64decode(value.encode("utf-8"))
    return int.from_bytes(raw, "big")


def _load_rsa_public_key_from_jwk(n: str, e: str = PUBLIC_E):
    return rsa.RSAPublicNumbers(
        e=_b64url_decode_int(e),
        n=_b64url_decode_int(n),
    ).public_key()


def _encrypt_password_code(password: str, public_n: str) -> str:
    public_key = _load_rsa_public_key_from_jwk(public_n, PUBLIC_E)
    encrypted = public_key.encrypt(
        password.encode("utf-8"),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(encrypted).decode("utf-8")


def _build_oopz_sign(*, path: str, body: str, oopz_time: str, private_key_pem: str) -> str:
    digest = hashlib.md5((path + body).encode("utf-8")).hexdigest()
    sign_input = (digest + oopz_time).encode("utf-8")
    private_key = _load_signing_private_key(private_key_pem)
    signature = private_key.sign(sign_input, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("utf-8")


def _build_password_login_body(
    *,
    phone: str,
    password: str,
    device_id: str,
    public_n: str,
) -> str:
    payload = {
        "auto": True,
        "code": _encrypt_password_code(password, public_n),
        "loginType": "PASSWORD",
        "phone": phone,
        "autoRegister": True,
        "deviceId": device_id,
        "deviceRam": "TBD",
        "deviceProcessor": "0",
        "loggedIn": device_id,
        "osEdition": "web",
        "osVersion": "web/BrowserName.chrome",
        "resolution": "TBD",
        "graphics": "TBD",
        "clientVersion": CLIENT_VERSION,
    }
    return _compact_json(payload)


def _build_password_login_headers(
    *,
    device_id: str,
    body: str,
    private_key_pem: str,
) -> dict[str, str]:
    oopz_time = _now_ms()
    return {
        "Accept": "*/*",
        "Content-Type": "application/json;charset=utf-8",
        "Oopz-App-Version-Number": APP_VERSION_NUMBER,
        "Oopz-Channel": OOPZ_CHANNEL,
        "Oopz-Device-Id": device_id,
        "Oopz-Platform": OOPZ_PLATFORM,
        "Oopz-Request-Id": str(uuid.uuid4()),
        "Oopz-Time": oopz_time,
        "Oopz-Web": "true",
        "Origin": "https://web.oopz.cn",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/147.0.0.0 Safari/537.36"
        ),
        "Oopz-Sign": _build_oopz_sign(
            path=LOGIN_RESPONSE_PATH,
            body=body,
            oopz_time=oopz_time,
            private_key_pem=private_key_pem,
        ),
    }


def _resolve_login_device_id(device_id: str | None = None) -> str:
    if device_id and str(device_id).strip():
        return str(device_id).strip()
    try:
        import config as runtime_config

        current = str(getattr(runtime_config, "OOPZ_CONFIG", {}).get("device_id") or "").strip()
        if current:
            return current
    except Exception:
        logger.debug("读取当前 OOPZ device_id 失败，登录时生成新设备 ID", exc_info=True)
    return str(uuid.uuid4())


def login_with_api_password(
    phone: str,
    password: str,
    *,
    device_id: str | None = None,
    timeout: float = 20,
) -> dict[str, Any]:
    """使用 OOPZ 登录接口直接换取本项目运行所需凭据。"""
    phone = str(phone or "").strip()
    password = str(password or "")
    if not phone or not password:
        raise OopzPasswordLoginError("账号和密码不能为空")

    resolved_device_id = _resolve_login_device_id(device_id)
    private_key_pem = get_client_signing_key()
    body = _build_password_login_body(
        phone=phone,
        password=password,
        device_id=resolved_device_id,
        public_n=get_client_password_modulus(),
    )
    headers = _build_password_login_headers(
        device_id=resolved_device_id,
        body=body,
        private_key_pem=private_key_pem,
    )

    try:
        response = requests.post(
            LOGIN_API_URL,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise OopzPasswordLoginError(f"OOPZ 登录请求失败: {exc}") from exc

    try:
        payload = response.json()
    except Exception as exc:
        raise OopzPasswordLoginError(
            f"OOPZ 登录接口返回非 JSON 响应: HTTP {response.status_code}"
        ) from exc

    if response.status_code >= 400 or not isinstance(payload, dict) or not payload.get("status"):
        raise OopzPasswordLoginError(
            _safe_response_error(payload),
            code=_extract_error_code(payload) or response.status_code,
            payload=payload,
        )

    data = payload.get("data")
    if not isinstance(data, dict):
        raise OopzPasswordLoginError("登录成功但响应 data 格式异常", payload=payload)

    person_uid = data.get("uid")
    jwt_token = data.get("signature")
    if not person_uid:
        raise OopzPasswordLoginError("登录成功但未返回 uid", payload=payload)
    if not jwt_token:
        raise OopzPasswordLoginError("登录成功但未返回 signature", payload=payload)

    return {
        "person_uid": str(person_uid),
        "device_id": resolved_device_id,
        "jwt_token": str(jwt_token),
        "private_key_pem": private_key_pem,
        "app_version": APP_VERSION_NUMBER,
    }


def _config_login_account(oopz_config: Mapping[str, Any]) -> tuple[str, str]:
    phone = (
        os.environ.get("OOPZ_PHONE")
        or oopz_config.get("login_phone")
        or oopz_config.get("phone")
        or ""
    )
    password = (
        os.environ.get("OOPZ_PASSWORD")
        or oopz_config.get("login_password")
        or oopz_config.get("password")
        or ""
    )
    return str(phone).strip(), str(password or "")


def refresh_credentials_from_config_password(
    *,
    timeout: float = 20,
    save: bool = True,
) -> dict[str, Any] | None:
    """配置里有 OOPZ 账号密码时，启动前用直接 API 刷新登录凭据。"""
    try:
        import config as runtime_config
    except Exception as exc:
        raise OopzPasswordLoginError(f"读取 config.py 失败: {exc}") from exc

    oopz_config = getattr(runtime_config, "OOPZ_CONFIG", {}) or {}
    phone, password = _config_login_account(oopz_config)
    if not phone or not password:
        return None

    credentials = login_with_api_password(phone, password, timeout=timeout)
    if save:
        save_credentials(credentials)
    return credentials


def _should_fallback_to_browser(exc: OopzPasswordLoginError) -> bool:
    if exc.code in (401, 403):
        return False
    message = str(exc)
    fatal_markers = ("该手机号尚未注册", "密码错误")
    if any(marker in message for marker in fatal_markers):
        return False
    text = message.lower()
    network_markers = ("请求失败", "超时", "timeout", "连接", "network", "json", "响应")
    if any(marker in text for marker in network_markers):
        return True
    return True


def _build_login_result(credentials: dict[str, Any], save: bool) -> dict[str, Any]:
    missing = _missing_required_credentials(credentials)
    if missing:
        raise OopzPasswordLoginError("登录成功但未捕获完整凭据: " + ", ".join(missing))

    saved: list[str] = []
    if save:
        saved = save_credentials(credentials)
    return {
        "ok": True,
        "saved": saved,
        "credentials": _sanitize_credentials(credentials),
        "raw": credentials,
        "restart_required": True,
    }


def _sanitize_credentials(credentials: dict[str, Any]) -> dict[str, Any]:
    jwt_info = _jwt_exp_info(str(credentials.get("jwt_token") or ""))
    return {
        "person_uid": _mask(credentials.get("person_uid")),
        "device_id": _mask(credentials.get("device_id")),
        "jwt_token": _mask(credentials.get("jwt_token"), keep=10),
        "private_key": bool(credentials.get("private_key_pem")),
        "app_version": credentials.get("app_version") or "",
        **jwt_info,
    }


def _update_from_headers(credentials: dict[str, Any], headers: dict[str, str]) -> None:
    if headers.get("oopz-person") and not credentials.get("person_uid"):
        credentials["person_uid"] = headers["oopz-person"]
    if headers.get("oopz-device-id") and not credentials.get("device_id"):
        credentials["device_id"] = headers["oopz-device-id"]
    if headers.get("oopz-signature") and not credentials.get("jwt_token"):
        credentials["jwt_token"] = headers["oopz-signature"]
    if headers.get("oopz-app-version-number"):
        credentials["app_version"] = headers["oopz-app-version-number"]


def _update_from_login_body(credentials: dict[str, Any], post_data: str | None) -> None:
    if not post_data:
        return
    try:
        body = json.loads(post_data)
    except Exception:
        return
    if isinstance(body, dict) and body.get("deviceId") and not credentials.get("device_id"):
        credentials["device_id"] = body["deviceId"]


def _apply_proxy_to_launch_kwargs(launch_kwargs: dict[str, Any]) -> None:
    try:
        from core.proxy_utils import get_playwright_proxy
        import config as runtime_config

        proxy = get_playwright_proxy(getattr(runtime_config, "OOPZ_CONFIG", {}).get("proxy"))
        if proxy:
            launch_kwargs["proxy"] = proxy
    except Exception:
        logger.debug("解析 OOPZ 登录浏览器代理失败，使用默认网络设置", exc_info=True)


def _build_launch_kwargs(headless: bool) -> dict[str, Any]:
    browser_env, crash_dir = _prepare_chromium_runtime()
    launch_kwargs: dict[str, Any] = {
        "user_data_dir": BROWSER_DATA_DIR,
        "headless": headless,
        "viewport": {"width": 1280, "height": 900},
        "locale": "zh-CN",
        "args": _chromium_args(crash_dir),
        "env": browser_env,
    }
    chromium_executable_path = _get_chromium_executable_path()
    if chromium_executable_path:
        launch_kwargs["executable_path"] = chromium_executable_path
    _apply_proxy_to_launch_kwargs(launch_kwargs)
    return launch_kwargs


async def _poll_private_key(page, credentials: dict[str, Any], seconds: float) -> None:
    deadline = time.monotonic() + max(0.1, seconds)
    while time.monotonic() < deadline:
        try:
            captured = await page.evaluate(JS_GET_CAPTURED)
            pem = (captured or {}).get("pem")
            if pem:
                credentials["private_key_pem"] = pem
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)


async def _clear_cached_keys_and_retry(page, credentials: dict[str, Any]) -> None:
    try:
        deleted = await page.evaluate(JS_CLEAR_INDEXEDDB)
        logger.info("OOPZ 登录私钥未捕获，已清理 IndexedDB 后重试: %s", deleted)
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        await _poll_private_key(page, credentials, 8)
    except Exception as exc:
        logger.debug("清理 IndexedDB 重试失败: %s", exc)


async def _open_clean_login_page(context, page) -> None:
    """清理旧网页登录态，确保本次使用表单里输入的账号。"""
    try:
        await context.clear_cookies()
    except Exception:
        logger.debug("清理 OOPZ Cookie 失败", exc_info=True)

    await page.goto(OOPZ_WEB_URL, wait_until="domcontentloaded")
    try:
        await page.evaluate(JS_CLEAR_INDEXEDDB)
    except Exception:
        logger.debug("清理 OOPZ 本地登录状态失败", exc_info=True)
    await page.goto(OOPZ_WEB_URL, wait_until="domcontentloaded")


async def _fill_password_login(page, phone: str, password: str) -> None:
    # OOPZ Web 是 Flutter Canvas，坐标点击比 DOM selector 更稳定。
    await page.mouse.click(880, 610)
    await page.wait_for_timeout(1000)
    await page.mouse.click(760, 354)
    await page.keyboard.press("Control+A")
    await page.keyboard.type(phone, delay=15)
    await page.mouse.click(760, 440)
    await page.keyboard.press("Control+A")
    await page.keyboard.type(password, delay=15)
    await page.mouse.click(882, 532)


async def login_with_password(
    phone: str,
    password: str,
    *,
    timeout: float = 90,
    headless: bool = True,
    save: bool = True,
) -> dict[str, Any]:
    """统一登录入口：先用 OOPZ 登录接口，失败时回退到浏览器登录。"""
    phone = str(phone or "").strip()
    password = str(password or "")
    if not phone or not password:
        raise OopzPasswordLoginError("账号和密码不能为空")

    try:
        api_credentials = await asyncio.to_thread(
            login_with_api_password,
            phone,
            password,
            timeout=min(max(float(timeout), 1.0), 20.0),
        )
        return _build_login_result(api_credentials, save)
    except OopzPasswordLoginError as exc:
        if not _should_fallback_to_browser(exc):
            raise
        logger.info("OOPZ API 登录失败，回退到浏览器登录: %s", exc)

    return await login_with_playwright_password(
        phone,
        password,
        timeout=timeout,
        headless=headless,
        save=save,
    )


async def login_with_playwright_password(
    phone: str,
    password: str,
    *,
    timeout: float = 90,
    headless: bool = True,
    save: bool = True,
) -> dict[str, Any]:
    """通过无头 Chromium 登录 OOPZ，并返回已脱敏的凭据摘要。"""
    phone = str(phone or "").strip()
    password = str(password or "")
    if not phone or not password:
        raise OopzPasswordLoginError("账号和密码不能为空")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise OopzPasswordLoginError("当前环境缺少 Playwright，请先安装依赖") from exc

    credentials = _new_credentials()
    login_done = asyncio.Event()
    login_error: dict[str, str] = {}

    async def on_response(response) -> None:
        if LOGIN_RESPONSE_PATH not in response.url:
            return
        try:
            payload = await response.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict) or not payload.get("status"):
            login_error["message"] = _safe_response_error(payload)
            login_done.set()
            return
        data = payload.get("data") or {}
        if isinstance(data, dict):
            if data.get("uid"):
                credentials["person_uid"] = data["uid"]
            if data.get("signature"):
                credentials["jwt_token"] = data["signature"]
        login_done.set()

    def on_request(request) -> None:
        try:
            headers = request.headers
            _update_from_headers(credentials, headers)
            if LOGIN_RESPONSE_PATH in request.url:
                _update_from_login_body(credentials, request.post_data)
        except Exception:
            logger.debug("解析 OOPZ 登录请求失败", exc_info=True)

    def on_websocket(ws) -> None:
        def on_frame(payload) -> None:
            try:
                data = json.loads(payload)
                if data.get("event") != WS_EVENT_AUTH:
                    return
                body = json.loads(data.get("body", "{}"))
                if body.get("person") and not credentials.get("person_uid"):
                    credentials["person_uid"] = body["person"]
                if body.get("deviceId") and not credentials.get("device_id"):
                    credentials["device_id"] = body["deviceId"]
                if body.get("signature") and not credentials.get("jwt_token"):
                    credentials["jwt_token"] = body["signature"]
            except Exception:
                pass

        ws.on("framesent", on_frame)

    os.makedirs(BROWSER_DATA_DIR, exist_ok=True)
    async with async_playwright() as p:
        launch_kwargs = _build_launch_kwargs(headless=headless)
        context = await p.chromium.launch_persistent_context(**launch_kwargs)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(30000)
            await page.add_init_script(JS_CRYPTO_HOOK)
            page.on("request", on_request)
            page.on("websocket", on_websocket)
            page.on("response", lambda response: asyncio.create_task(on_response(response)))

            await _open_clean_login_page(context, page)
            await page.wait_for_timeout(6500)
            await _fill_password_login(page, phone, password)

            try:
                await asyncio.wait_for(login_done.wait(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise OopzPasswordLoginError("等待 OOPZ 登录响应超时") from exc

            if login_error.get("message"):
                raise OopzPasswordLoginError(login_error["message"])

            await _poll_private_key(page, credentials, 10)
            if not credentials.get("private_key_pem"):
                await _clear_cached_keys_and_retry(page, credentials)
        finally:
            await context.close()

    return _build_login_result(credentials, save)


def _read_config_template() -> str:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return f.read()
    if os.path.exists(CONFIG_EXAMPLE_PATH):
        with open(CONFIG_EXAMPLE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    raise OopzPasswordLoginError("config.py 不存在，且未找到 config.example.py")


def _replace_config_value(content: str, key: str, value: Any) -> tuple[str, bool]:
    if value is None or str(value) == "":
        return content, False
    pattern = re.compile(rf'("{re.escape(key)}"\s*:\s*)"[^"]*"')
    replacement_value = json.dumps(str(value), ensure_ascii=False)
    content, count = pattern.subn(lambda m: f"{m.group(1)}{replacement_value}", content, count=1)
    return content, count > 0


def _private_key_module_content(pem: str) -> str:
    pem = pem.strip().replace("\r\n", "\n")
    return (
        '"""RSA 私钥（由后台 OOPZ 登录自动生成）"""\n'
        "\n"
        "from cryptography.hazmat.primitives import serialization\n"
        "from cryptography.hazmat.backends import default_backend\n"
        "\n"
        f'PRIVATE_KEY_PEM = b"""{pem}"""\n'
        "\n"
        "\n"
        "def get_private_key():\n"
        '    """加载并返回 RSA 私钥对象。"""\n'
        "    return serialization.load_pem_private_key(\n"
        "        PRIVATE_KEY_PEM,\n"
        "        password=None,\n"
        "        backend=default_backend(),\n"
        "    )\n"
    )


def _save_config(credentials: dict[str, Any]) -> str:
    content = _read_config_template()
    replaced_any = False
    for key in OOPZ_CONFIG_CREDENTIAL_FIELDS:
        content, replaced = _replace_config_value(content, key, credentials.get(key))
        replaced_any = replaced_any or replaced
    if not replaced_any:
        raise OopzPasswordLoginError("未能在 config.py 中定位 OOPZ_CONFIG 凭据字段")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    return "config.py"


def _save_private_key(pem: str) -> str:
    with open(PRIVATE_KEY_PATH, "w", encoding="utf-8") as f:
        f.write(_private_key_module_content(pem))
    return "private_key.py"


def save_credentials(credentials: dict[str, Any]) -> list[str]:
    """写入 config.py 与 private_key.py，不额外生成明文凭据备份。"""
    pem = str(credentials.get("private_key_pem") or "").strip()
    if not pem:
        raise OopzPasswordLoginError("缺少 RSA 私钥，无法写入 private_key.py")

    saved = [_save_config(credentials), _save_private_key(pem)]
    _apply_config_to_runtime(credentials)
    return saved


def _apply_config_to_runtime(credentials: dict[str, Any]) -> None:
    updates = {key: credentials.get(key) for key in OOPZ_CONFIG_CREDENTIAL_FIELDS if credentials.get(key)}
    for module_name in ("config", "web.web_player_config"):
        try:
            module = importlib.import_module(module_name)
            target = getattr(module, "OOPZ_CONFIG", None)
            if isinstance(target, dict):
                target.update(updates)
        except Exception:
            logger.debug("同步 %s.OOPZ_CONFIG 失败", module_name, exc_info=True)


def load_private_key_from_pem(pem: str):
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(
        pem.encode("utf-8"),
        password=None,
        backend=default_backend(),
    )
