from services.area_join_notifier import start_area_join_notifier
from bot.command_handler import CommandHandler
from oopz.oopz_client import OopzClient
from oopz.oopz_sender import OopzSender

from app.lifecycle.context import AppContext
from onebot_v11 import OneBotV11Service, get_onebot_v11_config


class AppContextBuilder:
    """负责组装启动期使用的应用上下文。"""

    def build(self, sender: OopzSender, voice=None) -> AppContext:
        notifier_callback = start_area_join_notifier(sender=sender)
        handler = CommandHandler(sender, voice_client=voice)
        onebot_config = get_onebot_v11_config()
        onebot_v11 = OneBotV11Service(sender, onebot_config) if onebot_config.enabled else None

        client = OopzClient(
            on_chat_message=handler.handle_message,
            on_other_event=notifier_callback,
            on_raw_event=onebot_v11.emit_raw_event if onebot_v11 else None,
        )

        return AppContext(
            sender=sender,
            handler=handler,
            client=client,
            notifier_callback=notifier_callback,
            onebot_v11=onebot_v11,
            voice=voice,
        )
