"""集中管理 HTTP 客户端的默认超时（秒）。

分档说明：
- ``HTTP_TIMEOUT_DEFAULT``：常规 API 请求（连接 + 读取共用单值）。
- ``HTTP_TIMEOUT_PROBE``：健康探测、内网回环等需要快速失败的轻量请求。
- ``HTTP_TIMEOUT_DOWNLOAD``：媒体下载 / 上传等可能较慢的大流量请求。
"""

from __future__ import annotations

HTTP_TIMEOUT_DEFAULT = 10
HTTP_TIMEOUT_PROBE = 3
HTTP_TIMEOUT_DOWNLOAD = 30
