"""
JSON 序列化工具。
"""

from __future__ import annotations

import json
from typing import Any


def compact_json(obj: Any) -> str:
    """紧凑 JSON：无多余空格分隔符，保留非 ASCII 原字符。

    Oopz 请求体要求「用于签名的字符串」与「实际发送的字符串」字节一致，
    故统一通过本函数生成，避免分隔符 / 转义差异导致签名失配。
    """
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
