"""Oopz 文件上传 Mixin — 图片、音频上传与发送。"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import os
import socket
from urllib.parse import urlparse

import requests
from PIL import Image

from core.http_constants import HTTP_TIMEOUT_DOWNLOAD, HTTP_TIMEOUT_MEDIA
from core.logger_config import get_logger
from core.proxy_utils import is_fake_ip

logger = get_logger("OopzUpload")

UPLOAD_PUT_TIMEOUT = (10, 60)

# 远程素材下载安全上限：防止超大响应一次性读进内存把进程打爆。
MAX_IMAGE_DOWNLOAD_BYTES = 20 * 1024 * 1024      # 20 MB
MAX_AUDIO_DOWNLOAD_BYTES = 100 * 1024 * 1024     # 100 MB
_DOWNLOAD_CHUNK = 64 * 1024


class RemoteFetchError(Exception):
    """远程素材下载被拒绝（SSRF 防护）或超出大小上限。"""


def _is_public_ip(ip) -> bool:
    """单个地址是否为公网地址。"""
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_public_host(host: str) -> bool:
    """域名 / IP 解析出的每个地址都必须是公网地址，否则视为不安全。

    两种情况分开处理：

    - host 本身是 IP 字面量：直接校验，不经 DNS。占位段地址同样按内网拒绝，
      因为无法反查它对应哪个域名。
    - host 是域名：解析后逐个校验，但跳过代理的 fake-ip 占位地址
      （见 ``core.proxy_utils.is_fake_ip``）—— 那只是 DNS 层的临时映射，
      真实解析由代理完成，据此判断内外网是错的。若解析结果全是占位地址，
      说明本机无从得知真实目标，交由代理决定路由。
    """
    try:
        return _is_public_ip(ipaddress.ip_address(host))
    except ValueError:
        pass  # 不是 IP 字面量，按域名走 DNS 解析

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False

    resolved_real_address = False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if is_fake_ip(ip):
            continue
        resolved_real_address = True
        if not _is_public_ip(ip):
            return False

    if not resolved_real_address:
        logger.debug("%s 仅解析出代理 fake-ip 占位地址，跳过公网校验，交由代理路由", host)
    return True


def _validate_remote_url(url: str) -> None:
    """拒绝非 http(s) 以及指向内网/环回/保留地址的 URL（SSRF 防护）。"""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise RemoteFetchError(f"不支持的 URL scheme: {parsed.scheme or '(空)'}")
    host = parsed.hostname or ""
    if not host:
        raise RemoteFetchError("URL 缺少主机名")
    # 注意：这里做的是解析期校验，无法完全防住 DNS rebinding，但能挡掉
    # 直接用内网域名 / IP 的 SSRF，对当前威胁模型足够。
    if not _is_public_host(host):
        raise RemoteFetchError(f"目标地址不是公网地址，已拒绝: {host}")


def _download_limited(session, url, *, max_bytes, timeout, headers=None) -> tuple[bytes, str]:
    """带 SSRF 校验与大小上限的流式下载，返回 (内容字节, Content-Type)。"""
    _validate_remote_url(url)
    resp = session.get(url, stream=True, timeout=timeout, headers=headers or {})
    try:
        resp.raise_for_status()
        declared = resp.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > max_bytes:
                    raise RemoteFetchError(f"远程文件过大: 声明 {declared} 字节 > 上限 {max_bytes}")
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise RemoteFetchError(f"远程文件超过大小上限 {max_bytes} 字节")
            chunks.append(chunk)
        return b"".join(chunks), resp.headers.get("Content-Type", "")
    finally:
        resp.close()


def get_image_info(file_path: str) -> tuple[int, int, int]:
    """获取本地图片的宽、高、文件大小"""
    with Image.open(file_path) as img:
        width, height = img.size
    file_size = os.path.getsize(file_path)
    return width, height, file_size


class UploadMixin:
    """Oopz 文件上传 Mixin — 图片、音频上传与发送。"""

    def upload_file(self, file_path: str, file_type: str = "IMAGE", ext: str = ".webp") -> dict:
        """上传本地文件，返回 { fileKey, url }"""
        url_path = "/rtc/v1/cos/v1/signedUploadUrl"
        body = {"type": file_type, "ext": ext}

        resp = self._put(url_path, body)
        if resp.status_code != 200:
            raise Exception(f"获取上传 URL 失败: {resp.text}")

        data = resp.json()["data"]
        upload_url = data["signedUrl"]
        file_key = data["file"]
        cdn_url = data["url"]

        with open(file_path, "rb") as f:
            put_resp = self.session.put(
                upload_url,
                data=f,
                headers={"Content-Type": "application/octet-stream"},
                timeout=UPLOAD_PUT_TIMEOUT,
            )
        if put_resp.status_code not in (200, 201):
            raise Exception(f"文件上传失败: {put_resp.text}")

        return {"fileKey": file_key, "url": cdn_url}

    def upload_file_from_url(self, image_url: str) -> dict:
        """从网络 URL 下载图片并上传到 Oopz（不落地磁盘）"""
        try:
            image_bytes, _content_type = _download_limited(
                self.session,
                image_url,
                max_bytes=MAX_IMAGE_DOWNLOAD_BYTES,
                timeout=HTTP_TIMEOUT_MEDIA,
            )

            img = Image.open(io.BytesIO(image_bytes))
            width, height = img.size
            file_size = len(image_bytes)
            ext = "." + (img.format or "webp").lower()
            md5 = hashlib.md5(image_bytes).hexdigest()

            url_path = "/rtc/v1/cos/v1/signedUploadUrl"
            body = {"type": "IMAGE", "ext": ext}
            resp2 = self._put(url_path, body)
            resp2.raise_for_status()
            data = resp2.json()["data"]

            signed_url = data["signedUrl"]
            file_key = data["file"]
            cdn_url = data["url"]

            put_resp = self.session.put(
                signed_url,
                data=image_bytes,
                headers={"Content-Type": "application/octet-stream"},
                timeout=UPLOAD_PUT_TIMEOUT,
            )
            put_resp.raise_for_status()

            attachment = {
                "fileKey": file_key,
                "url": cdn_url,
                "width": width,
                "height": height,
                "fileSize": file_size,
                "hash": md5,
                "animated": False,
                "displayName": "",
                "attachmentType": "IMAGE",
            }
            return {"code": "success", "message": "上传成功", "data": attachment}

        except Exception as e:
            logger.error(f"从 URL 上传失败: {e}")
            return {"code": "error", "message": str(e), "data": None}

    def upload_audio_from_url(
        self, audio_url: str, filename: str = "music.mp3", duration_ms: int = 0
    ) -> dict:
        """从网络 URL 下载音频并上传到 Oopz（AUDIO 类型）"""
        try:
            audio_bytes, content_type = _download_limited(
                self.session,
                audio_url,
                max_bytes=MAX_AUDIO_DOWNLOAD_BYTES,
                timeout=HTTP_TIMEOUT_DOWNLOAD,
                headers={"Referer": "https://music.163.com/"},
            )
            file_size = len(audio_bytes)
            if "mp4" in content_type or "m4a" in content_type:
                ext = ".m4a"
            elif "flac" in content_type:
                ext = ".flac"
            else:
                ext = ".mp3"

            md5 = hashlib.md5(audio_bytes).hexdigest()

            url_path = "/rtc/v1/cos/v1/signedUploadUrl"
            body = {"type": "AUDIO", "ext": ext}
            resp2 = self._put(url_path, body)
            resp2.raise_for_status()
            data = resp2.json()["data"]

            signed_url = data["signedUrl"]
            file_key = data["file"]
            cdn_url = data["url"]

            put_resp = self.session.put(
                signed_url,
                data=audio_bytes,
                headers={"Content-Type": "application/octet-stream"},
                timeout=UPLOAD_PUT_TIMEOUT,
            )
            put_resp.raise_for_status()

            base_name = os.path.splitext(filename or "")[0] or "music"
            display_name = base_name + ext
            duration_sec = duration_ms // 1000 if duration_ms else 0

            attachment = {
                "fileKey": file_key,
                "url": cdn_url,
                "fileSize": file_size,
                "hash": md5,
                "animated": False,
                "displayName": display_name,
                "attachmentType": "AUDIO",
                "duration": duration_sec,
            }
            logger.info(f"音频上传成功: {display_name} ({file_size} bytes, {duration_sec}s)")
            return {"code": "success", "data": attachment}

        except Exception as e:
            logger.error(f"音频上传失败: {e}")
            return {"code": "error", "message": str(e), "data": None}

    def upload_and_send_image(self, file_path: str, text: str = "", **kwargs) -> requests.Response:
        """上传本地图片并作为消息发送"""
        width, height, file_size = get_image_info(file_path)

        url_path = "/rtc/v1/cos/v1/signedUploadUrl"
        body = {"type": "IMAGE", "ext": os.path.splitext(file_path)[1]}
        resp = self._put(url_path, body)
        resp.raise_for_status()
        data = resp.json()["data"]

        signed_url = data["signedUrl"]
        file_key = data["file"]
        cdn_url = data["url"]

        with open(file_path, "rb") as f:
            self.session.put(
                signed_url,
                data=f,
                headers={"Content-Type": "application/octet-stream"},
                timeout=UPLOAD_PUT_TIMEOUT,
            ).raise_for_status()

        attachments = [{
            "fileKey": file_key,
            "url": cdn_url,
            "width": width,
            "height": height,
            "fileSize": file_size,
            "hash": "",
            "animated": False,
            "displayName": "",
            "attachmentType": "IMAGE",
        }]

        msg_text = f"![IMAGEw{width}h{height}]({file_key})"
        if text:
            msg_text += f"\n{text}"

        return self.send_message(text=msg_text, attachments=attachments, **kwargs)

    def upload_and_send_private_image(self, target: str, file_path: str, text: str = "") -> dict:
        """上传本地图片并通过私信发送。"""
        width, height, file_size = get_image_info(file_path)

        url_path = "/rtc/v1/cos/v1/signedUploadUrl"
        body = {"type": "IMAGE", "ext": os.path.splitext(file_path)[1]}
        try:
            resp = self._put(url_path, body)
            resp.raise_for_status()
            data = resp.json()["data"]
            signed_url = data["signedUrl"]
            file_key = data["file"]
            cdn_url = data["url"]

            with open(file_path, "rb") as f:
                self.session.put(
                    signed_url,
                    data=f,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=UPLOAD_PUT_TIMEOUT,
                ).raise_for_status()
        except Exception as e:
            logger.error(f"上传私信图片失败: {e}")
            return {"error": str(e)}

        attachment = {
            "fileKey": file_key,
            "url": cdn_url,
            "width": width,
            "height": height,
            "fileSize": file_size,
            "hash": "",
            "animated": False,
            "displayName": "",
            "attachmentType": "IMAGE",
        }
        msg_text = f"![IMAGEw{width}h{height}]({file_key})"
        if text:
            msg_text += f"\n{text}"
        result = self.send_private_message(target, msg_text, attachments=[attachment])
        if "error" in result:
            logger.error("私信图片发送失败: %s", result.get("error"))
            return result
        return {"status": True, "channel": result.get("channel"), "attachment": attachment}
