"""运行时访问器：延迟导入 ``web.web_player`` 暴露的实例与状态。

这些函数在调用时才导入 ``web.web_player``，避免管理后台模块与播放器主模块之间的
循环引用，是 shared 包内其它子模块的基础依赖。
"""

from __future__ import annotations


def _get_redis():
    """延迟导入，避免循环引用。"""
    from web.web_player import get_redis
    return get_redis()


def _get_sender():
    from web.web_player import get_sender
    return get_sender()


def _get_oopz_client():
    from web.web_player import get_oopz_client
    return get_oopz_client()


def _get_netease():
    from web.web_player import get_netease
    return get_netease()


def _get_started_at() -> float:
    from web.web_player import started_at
    return started_at


def _admin_enabled() -> bool:
    from web.web_player import _admin_enabled as web_admin_enabled
    return web_admin_enabled()


def _get_liked_ids_cache() -> list:
    from web.web_player import liked_ids_cache
    return liked_ids_cache


def _get_plugin_runtime():
    from web.web_player import get_plugin_runtime
    return get_plugin_runtime()


def _set_liked_ids_cache(value: list) -> None:
    import web.web_player as web_player
    web_player.liked_ids_cache = value


def _execute_control_action(action: str, body: dict, redis_client, area: str = "") -> dict:
    from web.web_player import execute_control_action
    return execute_control_action(action, body, redis_client, area=area)


def _execute_queue_action(action: str, index, redis_client, area: str = "") -> dict:
    from web.web_player import execute_queue_action
    return execute_queue_action(action, index, redis_client, area=area)


def _add_song_to_queue(body: dict, area: str = "") -> dict:
    from web.web_player import add_song_to_queue
    return add_song_to_queue(body, area=area)


__all__ = [
    "_get_redis",
    "_get_sender",
    "_get_oopz_client",
    "_get_netease",
    "_get_started_at",
    "_admin_enabled",
    "_get_liked_ids_cache",
    "_get_plugin_runtime",
    "_set_liked_ids_cache",
    "_execute_control_action",
    "_execute_queue_action",
    "_add_song_to_queue",
]
