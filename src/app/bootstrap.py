import signal

from app.lifecycle import (
    AppContext,
    AppContextBuilder,
    BackgroundServiceRunner,
    NeteaseApiRuntime,
    ShutdownCoordinator,
    StartupResourceBuilder,
    VoiceRuntimeBuilder,
)
from core.logger_config import setup_logger
from oopz.oopz_password_login import (
    OopzPasswordLoginError,
    refresh_credentials_from_config_password,
)

logger = setup_logger("Main")


class BotApplication:
    """负责组装并运行 Bot 应用。"""

    def __init__(self) -> None:
        self._netease_runtime = NeteaseApiRuntime()
        self._background_services = BackgroundServiceRunner()
        self._context_builder = AppContextBuilder()
        self._shutdown = ShutdownCoordinator()
        self._startup_resources = StartupResourceBuilder()
        self._voice_runtime = VoiceRuntimeBuilder()
        self._context: AppContext | None = None
        self._stop_requested = False
        self._shutdown_in_progress = False
        self._stop_signal: int | None = None

    @staticmethod
    def _warn_if_no_admins() -> None:
        """未配置管理员时明确告知后果与出路。

        权限判定是 fail-closed 的，空名单下所有管理命令对任何人都不可用；
        后台默认关闭、密码默认为空，也不能靠后台自救 —— 不提示的话，
        用户只会看到「无权限」而不知道该改哪里。
        """
        from config import ADMIN_UIDS

        if ADMIN_UIDS:
            return
        logger.warning("=" * 50)
        logger.warning("未配置 ADMIN_UIDS，所有管理命令对任何人都不可用。")
        logger.warning("在频道里发送 /setup 可查看你的 UID 与配置步骤。")
        logger.warning("=" * 50)

    def _install_signal_handlers(self) -> None:
        def _graceful_stop(signum, _frame):
            # Python 信号回调可在主线程持有业务锁时插入。这里只做
            # 不可逆状态写入和控制流中断，不记日志、不 join，也不调用
            # client.stop()；所有有预算的清理统一由 ShutdownCoordinator 执行。
            already_requested = self._stop_requested
            self._stop_requested = True
            if self._stop_signal is None:
                self._stop_signal = signum
            if not already_requested and not self._shutdown_in_progress:
                raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, _graceful_stop)
        signal.signal(signal.SIGINT, _graceful_stop)

    def run(self) -> None:
        logger.info("=" * 50)
        logger.info("Oopz Bot 正在启动...")
        logger.info("=" * 50)
        self._warn_if_no_admins()

        try:
            # 注册第一个 handler 后信号就可能立即到达，因此安装过程
            # 本身也必须在 finally 的保护范围内。
            self._install_signal_handlers()
            self._raise_if_stop_requested()
            self._netease_runtime.start()
            self._raise_if_stop_requested()
            self._refresh_oopz_credentials_from_config()
            self._raise_if_stop_requested()
            self._context = self._build_context()
            self._raise_if_stop_requested()
            self._background_services.start(self._context)
            self._raise_if_stop_requested()
            self._context.client.start()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

        logger.info("Oopz Bot 已停止。")

    def stop(self) -> None:
        self._stop_requested = True
        if self._shutdown_in_progress:
            return
        # 先公布关停已开始：此后再到达的信号只保留 stop request，
        # 不再抛 KeyboardInterrupt 打断 Coordinator 的数据库刷盘和资源回收。
        self._shutdown_in_progress = True
        if self._stop_signal is not None:
            logger.info("收到 %s，正在停止...", signal.Signals(self._stop_signal).name)
        self._shutdown.stop(
            self._context,
            self._netease_runtime,
            self._background_services,
        )

    def _raise_if_stop_requested(self) -> None:
        """在启动阶段边界阻止信号后继续启动下一项服务。"""
        if self._stop_requested:
            raise KeyboardInterrupt

    def _refresh_oopz_credentials_from_config(self) -> None:
        try:
            credentials = refresh_credentials_from_config_password()
        except OopzPasswordLoginError as exc:
            logger.warning("OOPZ 账号密码登录刷新失败，继续使用现有凭据: %s", exc)
            return
        except Exception as exc:
            logger.warning("OOPZ 账号密码登录刷新异常，继续使用现有凭据: %s", exc, exc_info=True)
            return
        if credentials:
            logger.info("已通过 OOPZ 账号密码刷新登录凭据")

    def _build_context(self) -> AppContext:
        resources = self._startup_resources.build()
        voice = self._voice_runtime.build()
        return self._context_builder.build(resources.sender, voice=voice)
