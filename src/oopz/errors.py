"""项目级 Oopz 错误。"""


class SensitiveContentError(RuntimeError):
    """消息被平台内容审核拒绝。"""


_SENSITIVE_REJECTION_KEYWORDS = ("敏感", "违规", "涉政", "涉黄")


def is_sensitive_rejection(message: str) -> bool:
    text = str(message or "")
    return any(keyword in text for keyword in _SENSITIVE_REJECTION_KEYWORDS)

