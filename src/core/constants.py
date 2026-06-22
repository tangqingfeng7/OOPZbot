"""
跨模块共享的常量
"""

from __future__ import annotations


class Msg:
    """消息状态前缀（事实上的消息样式枚举）。"""

    OK = "[ok]"
    ERR = "[x]"
    WARN = "[!]"
    INFO = "[info]"
    SYNC = "[sync]"
    PAINT = "[paint]"
    SEARCH = "[search]"


# ---------------------------------------------------------------------------
# Oopz @提及编码
# ---------------------------------------------------------------------------

MENTION_TEMPLATE = "(met){uid}(met)"
# 从文本中提取被提及的 uid
MENTION_PATTERN = r"\(met\)(\w+)\(met\)"
# Oopz 用户 uid 形态（32 位十六进制）
UID_PATTERN = r"[a-f0-9]{32}"


def build_mention(uid: str) -> str:
    """构造 Oopz @提及标记。"""
    return MENTION_TEMPLATE.format(uid=uid)


# ---------------------------------------------------------------------------
# HTTP User-Agent
# ---------------------------------------------------------------------------

# 统一的浏览器 UA，供各处出站 HTTP 请求复用，避免 Chrome 版本号在多文件漂移。
# 与 config.DEFAULT_HEADERS 中的 UA 保持一致。
CHROME_VERSION = "140.0.0.0"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{CHROME_VERSION} Safari/537.36"
)


# ---------------------------------------------------------------------------
# 插件
# ---------------------------------------------------------------------------

PLUGINS_DIR_NAME = "plugins"
DEFAULT_PLUGIN_CONFIG_FILENAME = "config.json"
