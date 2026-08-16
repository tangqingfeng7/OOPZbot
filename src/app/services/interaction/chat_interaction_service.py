from app.services.runtime import CommandRuntimeView, sender_of
from config import CHAT_CONFIG


class ChatInteractionService:
    """负责关键词自动回复和未知命令提示。"""

    def __init__(self, runtime: CommandRuntimeView):
        self._sender = sender_of(runtime)

    @staticmethod
    def _try_reply(content: str) -> str | None:
        if not CHAT_CONFIG.get("enabled", True) or not content:
            return None
        replies = CHAT_CONFIG.get("keyword_replies") or {}
        content_lower = content.strip().lower()
        for keyword, reply in replies.items():
            if str(keyword).lower() == content_lower:
                return str(reply)
        for keyword, reply in replies.items():
            if str(keyword).lower() in content_lower:
                return str(reply)
        return None

    async def handle_plain_chat(self, content: str, channel: str, area: str) -> bool:
        """处理非命令消息的自动回复。"""
        reply = self._try_reply(content)
        if not reply:
            return False

        await self._sender.send_message(reply, channel=channel, area=area)
        return True

    async def send_unknown_mention_command(self, text: str, channel: str, area: str, suggestions: list[str] | None = None) -> None:
        """发送未知 @bot 命令提示。"""
        lines = [f"未识别的命令: {text}", "输入 @bot 帮助 查看分类帮助"]
        if suggestions:
            lines.append("你是不是想用:")
            for item in suggestions:
                lines.append(f"  {item}")
        await self._sender.send_message("\n".join(lines), channel=channel, area=area)

    async def send_unknown_command(self, command: str, channel: str, area: str, suggestions: list[str] | None = None) -> None:
        """发送未知斜杠命令提示。"""
        lines = [f"未知命令: {command}", "输入 /help 查看帮助"]
        if suggestions:
            lines.append("你是不是想用:")
            for item in suggestions:
                lines.append(f"  {item}")
        await self._sender.send_message("\n".join(lines), channel=channel, area=area)
