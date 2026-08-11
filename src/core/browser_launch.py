"""共享的 Chromium 启动参数。

把语音推流与登录页共用的 Chromium 启动参数集中到 core 层，作为单一来源，
供项目级浏览器后备能力复用，避免启动参数在各模块重复维护。
"""

from __future__ import annotations

# 语音推流用的 Chromium 参数：媒体自动播放 + 关闭沙箱 + 直连（不走代理）。
VOICE_STREAM_ARGS: list[str] = [
    "--disable-web-security",
    "--allow-file-access-from-files",
    "--autoplay-policy=no-user-gesture-required",
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--no-proxy-server",
]

# 登录页相对推流额外需要的参数（反自动化检测 / 关闭崩溃上报等）。
_LOGIN_EXTRA_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
    "--autoplay-policy=no-user-gesture-required",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-crash-reporter",
    "--disable-crashpad",
)


def login_browser_args() -> list[str]:
    """登录页用的 Chromium 参数。

    在推流参数基础上去掉 ``--no-proxy-server``（登录需遵循 OOPZ/系统代理设置），
    再补充反自动化检测与崩溃上报关闭等参数。
    """
    args = [arg for arg in VOICE_STREAM_ARGS if arg != "--no-proxy-server"]
    for extra in _LOGIN_EXTRA_ARGS:
        if extra not in args:
            args.append(extra)
    return args
