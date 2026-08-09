import time
from collections.abc import Callable

from app.lifecycle.context import AppContext
from core.logger_config import setup_logger
from web import web_player_config as web_cfg
from web.web_player import WebPlayerService

logger = setup_logger("BackgroundServices")


class BackgroundServiceRunner:
    """负责启动命令链路依赖的后台线程与监听器。"""

    def __init__(self) -> None:
        self._web_player: WebPlayerService | None = None
        self._context: AppContext | None = None

    def start(self, context: AppContext) -> None:
        self._context = context
        self._start_onebot_v11(context)
        self._start_music_services(context)
        self._start_web_player(context)
        self._start_scheduler_services(context)

    def _start_onebot_v11(self, context: AppContext) -> None:
        if not context.onebot_v11:
            return
        context.onebot_v11.start()
        logger.info("OneBot v11 旁路服务已启动。")

    def _start_music_services(self, context: AppContext) -> None:
        music = context.handler.infrastructure.music
        music.start_auto_play_monitor()
        music.start_web_command_listener()
        logger.info("自动播放监控已启动。")

    def _start_web_player(self, context: AppContext) -> None:
        from web.web_player import register_runtime_dependencies, set_oopz_client, set_sender
        set_sender(context.sender)
        set_oopz_client(context.client)
        register_runtime_dependencies(
            music=context.handler.infrastructure.music,
            plugins=context.handler.infrastructure.plugins,
            plugin_host=context.handler.plugin_host,
        )
        self._warmup_members_cache(context.sender)
        web_host = web_cfg.web_host()
        web_port = web_cfg.web_port()
        self._web_player = WebPlayerService(host=web_host, port=web_port)
        self._web_player.start()
        logger.info("Web 播放器已启动: http://%s:%s", web_host, web_port)
        logger.info("WebSocket 客户端启动中...")

    def _warmup_members_cache(self, sender) -> None:
        try:
            from core.area_config import get_area_registry
            registry = get_area_registry()
            area_ids = registry.get_all_area_ids()
            if not area_ids:
                from config import OOPZ_CONFIG
                fallback = (OOPZ_CONFIG.get("default_area") or "").strip()
                if not fallback:
                    areas = sender.get_joined_areas(quiet=True)
                    if areas:
                        fallback = (areas[0].get("id") or "").strip()
                area_ids = [fallback] if fallback else []
            total = 0
            for area in area_ids:
                if not area:
                    continue
                result = sender.get_area_members(area=area, quiet=True)
                if "error" not in result:
                    total += result.get("fetchedCount", 0)
                else:
                    logger.debug("成员缓存预热失败 (area=%s): %s", area[:8], result.get("error"))
            if total:
                logger.info("成员缓存预热完成: %d 个域, 共 %d 人", len(area_ids), total)
        except Exception:
            logger.debug("成员缓存预热异常", exc_info=True)

    def _start_scheduler_services(self, context: AppContext) -> None:
        try:
            scheduler = context.handler.services.scheduler
            scheduler.scheduled.start()
            scheduler.reminder.start()
        except Exception:
            logger.warning("定时消息/提醒服务启动失败", exc_info=True)

    @staticmethod
    def _attempt_stop(name: str, callback: Callable[[], object]) -> None:
        try:
            callback()
        except Exception as exc:
            logger.warning("停止 %s 时出现异常: %s", name, exc)

    def stop_ingress(self, timeout: float = 5.0) -> None:
        """停止新的外部输入；可重复调用。"""
        deadline = time.monotonic() + max(0.0, timeout)
        context = self._context
        if context:
            self._attempt_stop(
                "Oopz",
                lambda: context.client.stop(
                    timeout=max(0.0, deadline - time.monotonic())
                ),
            )
            onebot_v11 = context.onebot_v11
            if onebot_v11:
                self._attempt_stop(
                    "OneBot v11",
                    lambda: onebot_v11.stop(
                        timeout=max(0.0, deadline - time.monotonic())
                    ),
                )
        web_player = self._web_player
        if web_player:
            self._attempt_stop(
                "Web 播放器",
                lambda: web_player.stop(
                    timeout=max(0.0, deadline - time.monotonic())
                ),
            )

    def stop_producers(self, timeout: float = 5.0) -> None:
        """停止非插件轮询、监听与定时生产者；可重复调用。"""
        deadline = time.monotonic() + max(0.0, timeout)
        context = self._context
        if not context:
            return
        self._attempt_stop(
            "音乐后台服务",
            lambda: context.handler.infrastructure.music.stop(
                timeout=max(0.0, deadline - time.monotonic())
            ),
        )
        notifier = context.notifier_callback
        if notifier and hasattr(notifier, "stop"):
            self._attempt_stop(
                "区域通知",
                lambda: notifier.stop(
                    timeout=max(0.0, deadline - time.monotonic())
                ),
            )
        self._attempt_stop(
            "定时消息",
            lambda: context.handler.services.scheduler.scheduled.stop(
                timeout=max(0.0, deadline - time.monotonic())
            ),
        )
        self._attempt_stop(
            "提醒",
            lambda: context.handler.services.scheduler.reminder.stop(
                timeout=max(0.0, deadline - time.monotonic())
            ),
        )
        self._attempt_stop(
            "自动撤回",
            lambda: context.handler.services.safety.recall_scheduler.stop(
                timeout=max(0.0, deadline - time.monotonic())
            ),
        )

    def stop_plugins(self, timeout: float = 5.0) -> None:
        """在 dispatcher 排空后卸载插件，保留已入队插件命令的处理能力。"""
        context = self._context
        if not context:
            return
        self._attempt_stop(
            "插件",
            lambda: context.handler.infrastructure.plugins.stop(
                timeout=max(0.0, timeout)
            ),
        )

    def stop(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        self.stop_ingress(timeout=max(0.0, deadline - time.monotonic()))
        self.stop_producers(timeout=max(0.0, deadline - time.monotonic()))
        self.stop_plugins(timeout=max(0.0, deadline - time.monotonic()))
