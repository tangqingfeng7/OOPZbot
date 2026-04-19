"""Oopz 文件上传 Mixin — 图片、音频上传与发送。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from typing import TYPE_CHECKING, Optional

import requests
from PIL import Image

from config import OOPZ_CONFIG
from logger_config import get_logger

if TYPE_CHECKING:
    from oopz_sender import OopzSender

logger = get_logger("OopzUpload")

UPLOAD_PUT_TIMEOUT = (10, 60)
DOWNLOAD_TIMEOUT = (10, 30)
STREAM_CHUNK_SIZE = 64 * 1024


def get_image_info(file_path: str) -> tuple[int, int, int]:
    """获取本地图片的宽、高、文件大小"""
    with Image.open(file_path) as img:
        width, height = img.size
    file_size = os.path.getsize(file_path)
    return width, height, file_size


class UploadMixin:
    """Oopz 文件上传 Mixin — 图片、音频上传与发送。"""

    @staticmethod
    def _pick_audio_ext(content_type: str) -> str:
        if "mp4" in content_type or "m4a" in content_type:
            return ".m4a"
        if "flac" in content_type:
            return ".flac"
        return ".mp3"

    def _request_signed_upload(self, *, file_type: str, ext: str) -> dict:
        resp = self._put("/rtc/v1/cos/v1/signedUploadUrl", {"type": file_type, "ext": ext})
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        signed_url = data.get("signedUrl")
        file_key = data.get("file")
        cdn_url = data.get("url")
        if not signed_url or not file_key or not cdn_url:
            raise ValueError("签名上传地址响应缺少必要字段")
        return {"signed_url": signed_url, "file_key": file_key, "cdn_url": cdn_url}

    def _download_to_spooled_file(
        self,
        url: str,
        *,
        timeout: int | tuple[int, int],
        headers: Optional[dict] = None,
        stream: bool = False,
    ) -> dict:
        with self.session.get(url, stream=stream, timeout=timeout, headers=headers) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")

            if not stream:
                raw = resp.content
                fp = tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024)
                fp.write(raw)
                fp.seek(0)
                return {
                    "file": fp,
                    "size": len(raw),
                    "md5": hashlib.md5(raw).hexdigest(),
                    "content_type": content_type,
                }

            fp = tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024)
            digest = hashlib.md5()
            size = 0
            for chunk in resp.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                if not chunk:
                    continue
                fp.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            fp.seek(0)
            return {
                "file": fp,
                "size": size,
                "md5": digest.hexdigest(),
                "content_type": content_type,
            }

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

    def upload_file_from_url(self, image_url: str, *, stream: bool = False) -> dict:
        """从网络 URL 下载图片并上传到 Oopz（不落地磁盘）"""
        try:
            downloaded = self._download_to_spooled_file(
                image_url, timeout=DOWNLOAD_TIMEOUT, stream=stream
            )
            fp = downloaded["file"]
            with Image.open(fp) as img:
                width, height = img.size
                ext = "." + (img.format or "webp").lower()

            upload_target = self._request_signed_upload(file_type="IMAGE", ext=ext)
            fp.seek(0)
            put_resp = self.session.put(
                upload_target["signed_url"],
                data=fp,
                headers={"Content-Type": "application/octet-stream"},
                timeout=UPLOAD_PUT_TIMEOUT,
            )
            put_resp.raise_for_status()
            fp.close()

            attachment = {
                "fileKey": upload_target["file_key"],
                "url": upload_target["cdn_url"],
                "width": width,
                "height": height,
                "fileSize": downloaded["size"],
                "hash": downloaded["md5"],
                "animated": False,
                "displayName": "",
                "attachmentType": "IMAGE",
            }
            return {"code": "success", "message": "上传成功", "data": attachment}

        except Exception as e:
            logger.error(f"从 URL 上传失败: {e}")
            return {"code": "error", "message": str(e), "data": None}

    def upload_audio_from_url(
        self,
        audio_url: str,
        filename: str = "music.mp3",
        duration_ms: int = 0,
        *,
        stream: bool = False,
    ) -> dict:
        """从网络 URL 下载音频并上传到 Oopz（AUDIO 类型）"""
        try:
            downloaded = self._download_to_spooled_file(
                audio_url,
                timeout=30,
                headers={"Referer": "https://music.163.com/"},
                stream=stream,
            )
            ext = self._pick_audio_ext(downloaded["content_type"])
            upload_target = self._request_signed_upload(file_type="AUDIO", ext=ext)
            downloaded["file"].seek(0)
            put_resp = self.session.put(
                upload_target["signed_url"],
                data=downloaded["file"],
                headers={"Content-Type": "application/octet-stream"},
                timeout=UPLOAD_PUT_TIMEOUT,
            )
            put_resp.raise_for_status()
            downloaded["file"].close()

            base_name = os.path.splitext(filename or "")[0] or "music"
            display_name = base_name + ext
            duration_sec = duration_ms // 1000 if duration_ms else 0

            attachment = {
                "fileKey": upload_target["file_key"],
                "url": upload_target["cdn_url"],
                "fileSize": downloaded["size"],
                "hash": downloaded["md5"],
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
