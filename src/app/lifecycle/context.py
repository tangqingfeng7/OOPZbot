from dataclasses import dataclass
from typing import Any, Optional

from bot.command_handler import CommandHandler
from oopz.oopz_client import OopzClient
from oopz.oopz_sender import OopzSender
from music.voice_client import VoiceClient


@dataclass
class AppContext:
    """保存启动层创建的长生命周期服务。"""

    sender: OopzSender
    handler: CommandHandler
    client: OopzClient
    notifier_callback: Optional[Any] = None
    onebot_v11: Optional[Any] = None
    voice: Optional[VoiceClient] = None
