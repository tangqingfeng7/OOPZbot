"""集中管理 HTTP 客户端的默认超时（秒）。

分档说明：
- ``HTTP_TIMEOUT_HEALTH``：进程就绪轮询等需要尽快失败重试的内网健康探测。
- ``HTTP_TIMEOUT_PROBE``：诊断 / 内网回环等轻量探测。
- ``HTTP_TIMEOUT_DEFAULT``：常规 API 请求（连接 + 读取共用单值）。
- ``HTTP_TIMEOUT_MEDIA``：图片等中等体积资源下载。
- ``HTTP_TIMEOUT_LOGIN``：启动时账号密码登录 / 凭据刷新。
- ``HTTP_TIMEOUT_DOWNLOAD``：音频等较慢的大流量下载 / 上传。

注：AI 审核 / 推理等领域特定超时由 ``services/chat.py`` 自带的局部常量管理，
不在这里集中（语义与通用 HTTP 客户端不同）。
"""

from __future__ import annotations

HTTP_TIMEOUT_HEALTH = 2
HTTP_TIMEOUT_PROBE = 3
HTTP_TIMEOUT_DEFAULT = 10
HTTP_TIMEOUT_MEDIA = 15
HTTP_TIMEOUT_LOGIN = 20
HTTP_TIMEOUT_DOWNLOAD = 30
