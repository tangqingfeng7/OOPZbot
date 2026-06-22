"""
Oopz 请求签名的唯一来源。
签名算法（oopz_sender / name_resolver / oopz_password_login 三处调用点完全一致）：

    MD5(path + body) 的十六进制摘要 + oopz_time → RSA PKCS1v15 + SHA256 → Base64
"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from typing import Dict, Mapping

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding


def rsa_sign(private_key, data: str) -> str:
    """RSA PKCS1v15 + SHA256 签名，返回 Base64 字符串。"""
    sig = private_key.sign(
        data.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def build_oopz_sign(private_key, path: str, body: str, oopz_time: str) -> str:
    """构造 Oopz-Sign 值：先取 MD5(path + body) 拼上时间戳，再 RSA 签名。"""
    digest = hashlib.md5((path + body).encode("utf-8")).hexdigest()
    return rsa_sign(private_key, digest + oopz_time)


def oopz_auth_headers(
    private_key, config: Mapping[str, object], path: str, body: str
) -> Dict[str, str]:
    """构造 Oopz 鉴权请求头（10 个 Oopz-* 字段）的唯一来源。

    时间戳同时用于签名与 Oopz-Time 头，二者必须一致，故在此一次性生成。
    不包含 DEFAULT_HEADERS 等基础头，由调用方按需合并。
    """
    ts = str(int(time.time() * 1000))
    return {
        "Oopz-Sign": build_oopz_sign(private_key, path, body, ts),
        "Oopz-Request-Id": str(uuid.uuid4()),
        "Oopz-Time": ts,
        "Oopz-App-Version-Number": config["app_version"],
        "Oopz-Channel": config["channel"],
        "Oopz-Device-Id": config["device_id"],
        "Oopz-Platform": config["platform"],
        "Oopz-Web": str(config["web"]).lower(),
        "Oopz-Person": config["person_uid"],
        "Oopz-Signature": config["jwt_token"],
    }
