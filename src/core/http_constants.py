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

# --- 元组档位：requests 的 timeout=(连接超时, 读超时) ---
#
# 连接超时与这个请求要传多少数据无关，各档共用同一个短值，分档只分「读」。
# 读超时是**相邻两次收到数据之间**的最大空档，不是整个请求的总耗时上限 ——
# 它挡的是「连上了但服务端不回包 / 传到一半卡死」。requests 没有总时限概念，
# 需要总时限由调用方控制（本项目靠 RetryPolicy 的次数）。
HTTP_CONNECT_TIMEOUT = 5

HttpTimeout = float | tuple[float, float] | None

# 常规签名 API：小 JSON 请求 / 响应
HTTP_TIMEOUT_API: tuple[float, float] = (HTTP_CONNECT_TIMEOUT, HTTP_TIMEOUT_DEFAULT)
# 批量 / 搜索类：一次问几十个 uid、一次拉全部语音频道，服务端本来就慢
HTTP_TIMEOUT_API_SLOW: tuple[float, float] = (HTTP_CONNECT_TIMEOUT, HTTP_TIMEOUT_MEDIA)
