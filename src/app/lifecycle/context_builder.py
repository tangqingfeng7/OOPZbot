from app.lifecycle.context import AppContext
from bot.command_handler import CommandHandler
from core.logger_config import get_logger
from core.message_dispatcher import MessageDispatcher
from onebot_v11 import OneBotV11Service, get_onebot_v11_config
from oopz.oopz_client import OopzClient
from oopz.oopz_sender import OopzSender, SensitiveContentError
from services.area_join_notifier import start_area_join_notifier

logger = get_logger("AppContext")


def build_ws_credential_refresher(sender: OopzSender):
    """给 OopzClient 用的凭据刷新回调：账号密码重登并同步发送端签名私钥。"""

    def _refresh():
        from oopz.oopz_password_login import (
            OopzPasswordLoginError,
            load_private_key_from_pem,
            refresh_credentials_from_config_password,
        )

        try:
            credentials = refresh_credentials_from_config_password(save=True)
        except OopzPasswordLoginError as exc:
            logger.warning("WS 凭据自动刷新失败: %s", exc)
            return None
        except Exception:
            logger.warning("WS 凭据自动刷新异常", exc_info=True)
            return None
        if not credentials:
            logger.warning("未配置 login_phone/login_password，无法自动刷新 WS 凭据")
            return None
        pem = str(credentials.get("private_key_pem") or "").strip()
        if pem:
            try:
                sender.signer.private_key = load_private_key_from_pem(pem)
            except Exception:
                logger.warning("WS 凭据刷新后更新发送端签名私钥失败", exc_info=True)
        return credentials

    return _refresh


class AppContextBuilder:
    """负责组装启动期使用的应用上下文。"""

    def build(self, sender: OopzSender, voice=None) -> AppContext:
        handler = CommandHandler(sender, voice_client=voice)
        sender.bind_auto_recall_scheduler(
            handler.services.safety.recall_scheduler
        )
        onebot_config = get_onebot_v11_config()
        onebot_v11 = OneBotV11Service(sender, onebot_config) if onebot_config.enabled else None
        notifier_callback = start_area_join_notifier(
            sender=sender,
            on_member_change=onebot_v11.emit_member_change if onebot_v11 else None,
        )

        # WS 接收线程只负责解析入队；命令处理（AI 请求、音乐搜索等慢操作）
        # 由分发器的工作线程执行。按 area:channel 分片，保证单频道内顺序。
        dispatcher = MessageDispatcher(workers=4, maxsize=512)
        dispatcher.start()

        def _handle_chat_task(msg_data: dict) -> None:
            try:
                handler.handle_message(msg_data)
            except SensitiveContentError:
                logger.debug("回复被平台风控拦截，已忽略（发送层已记录）")

        def _dispatch_chat(msg_data: dict) -> None:
            key = f"{msg_data.get('area') or ''}:{msg_data.get('channel') or ''}"
            dispatcher.submit(key, _handle_chat_task, msg_data)

        def _dispatch_other_event(event: int, data: dict) -> None:
            if notifier_callback is None:
                return
            dispatcher.submit("__other_events__", notifier_callback, event, data)

        client = OopzClient(
            on_chat_message=_dispatch_chat,
            on_other_event=_dispatch_other_event if notifier_callback else None,
            on_raw_event=onebot_v11.emit_raw_event if onebot_v11 else None,
            credential_refresher=build_ws_credential_refresher(sender),
        )

        return AppContext(
            sender=sender,
            handler=handler,
            client=client,
            notifier_callback=notifier_callback,
            onebot_v11=onebot_v11,
            voice=voice,
            dispatcher=dispatcher,
        )
