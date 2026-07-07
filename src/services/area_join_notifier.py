import re
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple, Callable, Set

from config import OOPZ_CONFIG
from oopz.oopz_sender import OopzSender
from core.constants import build_mention
from core.logger_config import get_logger

logger = get_logger("AreaJoinNotifier")


EVENT_SOURCE_OPERATE_LOGS = "operate_logs"
EVENT_SOURCE_MEMBER_SNAPSHOT = "member_snapshot"
EVENT_SOURCE_CHOICES = (EVENT_SOURCE_OPERATE_LOGS, EVENT_SOURCE_MEMBER_SNAPSHOT)
OPERATE_LOG_MEMBER_OP_TYPES = ["AREA_SUBSCRIBE", "AREA_UNSUBSCRIBE"]
_JOIN_CONTENT = "\u52a0\u5165\u57df"
_LEAVE_CONTENT = "\u9000\u51fa\u57df"


@dataclass(frozen=True)
class AreaMemberChange:
    action: str
    area: str
    uid: str
    create_time: int
    content: str

    @property
    def key(self) -> tuple[int, str, str, str]:
        return (self.create_time, self.uid, self.action, self.content)


class AreaOperateLogCursor:
    """记录已消费的域管理日志，避免重复触发成员事件。"""

    def __init__(self, max_seen_per_area: int = 200):
        self.max_seen_per_area = max(20, int(max_seen_per_area))
        self._initialized: set[str] = set()
        self._seen: dict[str, set[tuple[int, str, str, str]]] = {}
        self._order: dict[str, list[tuple[int, str, str, str]]] = {}

    def consume(self, area: str, changes: list[AreaMemberChange]) -> list[AreaMemberChange]:
        area = str(area or "").strip()
        ordered = sorted(changes, key=lambda c: (c.create_time, c.uid, c.action))
        if not area:
            return []
        if area not in self._initialized:
            for change in ordered:
                self._mark_seen(area, change.key)
            self._initialized.add(area)
            return []

        fresh: list[AreaMemberChange] = []
        seen = self._seen.setdefault(area, set())
        for change in ordered:
            if change.key in seen:
                continue
            fresh.append(change)
            self._mark_seen(area, change.key)
        return fresh

    def _mark_seen(self, area: str, key: tuple[int, str, str, str]) -> None:
        seen = self._seen.setdefault(area, set())
        order = self._order.setdefault(area, [])
        if key in seen:
            return
        seen.add(key)
        order.append(key)
        while len(order) > self.max_seen_per_area:
            old = order.pop(0)
            seen.discard(old)


_UID_LIKE = re.compile(
    r"^[0-9a-fA-F]{8}([0-9a-fA-F]*|[\.…]+)$|^[0-9a-fA-F]{1,12}[\.…]+$"
)


def _looks_like_uid(name: str) -> bool:
    if not name or len(name) > 32:
        return False
    s = name.strip()
    if not s:
        return False
    return bool(_UID_LIKE.match(s))


def _resolve_display_name(sender: OopzSender, uid: str, cached: Optional[str] = None) -> str:
    if cached and not _looks_like_uid(cached):
        return cached
    try:
        detail = sender.get_person_detail_full(uid)
        if "error" not in detail:
            for key in ("name", "nickname", "displayName", "userName"):
                val = detail.get(key)
                if val and isinstance(val, str) and val.strip():
                    return val.strip()
        detail = sender.get_person_detail(uid)
        if "error" not in detail:
            for key in ("name", "nickname", "displayName", "userName"):
                val = detail.get(key)
                if val and isinstance(val, str) and val.strip():
                    return val.strip()
    except Exception:
        pass
    return cached or (uid[:8] + "…" if len(uid) > 8 else uid)


from oopz.area_events import parse_member_event as _parse_member_event


_area_channel_cache: dict = {"area": "", "channel": "", "ts": 0.0}
_AREA_CHANNEL_CACHE_TTL = 300.0  # 5 分钟
# 轮询线程与 WS 事件回调线程会并发读写该缓存，统一用锁保护读改写。
_area_channel_cache_lock = threading.Lock()


def _read_area_channel_cache(now: float) -> Optional[Tuple[str, str]]:
    with _area_channel_cache_lock:
        if _area_channel_cache["area"] and _area_channel_cache["channel"] \
                and now - _area_channel_cache["ts"] < _AREA_CHANNEL_CACHE_TTL:
            return _area_channel_cache["area"], _area_channel_cache["channel"]
    return None


def _store_area_channel_cache(area: str, channel: str, ts: float) -> None:
    with _area_channel_cache_lock:
        _area_channel_cache.update(area=area, channel=channel, ts=ts)


def _get_default_area_channel(sender: OopzSender, quiet: bool = False) -> Tuple[str, str]:
    """获取默认域 ID 和文字频道 ID（与 WS 通知逻辑一致）。quiet=True 时不打域/频道列表日志。"""
    default_area = (OOPZ_CONFIG.get("default_area") or "").strip()
    default_channel = (OOPZ_CONFIG.get("default_channel") or "").strip()
    if default_area and default_channel:
        return default_area, default_channel

    now = time.time()
    cached = _read_area_channel_cache(now)
    if cached is not None:
        return cached

    areas = sender.get_joined_areas(quiet=quiet)
    if areas:
        default_area = (areas[0].get("id") or "").strip()
    if default_area:
        for g in sender.get_area_channels(area=default_area, quiet=quiet):
            for ch in (g.get("channels") or []):
                if (ch.get("type") or "").upper() != "VOICE":
                    default_channel = (ch.get("id") or "").strip()
                    if default_channel:
                        _store_area_channel_cache(default_area, default_channel, now)
                        return default_area, default_channel
    if default_area and default_channel:
        _store_area_channel_cache(default_area, default_channel, now)
    return default_area, default_channel


def _member_uid(m: dict) -> str:
    """从成员项中取出 uid。"""
    if not isinstance(m, dict):
        return ""
    return (m.get("uid") or m.get("id") or m.get("person") or m.get("personId") or "").strip() or ""


def _next_poll_interval(base_interval: int, current_interval: int, rate_limited: bool) -> int:
    """根据是否被限流，计算下一次轮询间隔。"""
    base = max(5, int(base_interval))
    current = max(base, int(current_interval))
    if not rate_limited:
        return base
    return min(max(current * 2, base), 60)


def fetch_member_uid_snapshot(
    sender: OopzSender,
    area: str,
    member_fetch_max: int = 5000,
) -> Tuple[Optional[Set[str]], bool, bool]:
    """分页拉取域成员快照。

    返回 ``(uids, rate_limited, truncated)``：
    - uids 为 None 表示本次拉取失败（rate_limited 标记是否为限流）；
    - truncated=True 表示成员数超过 member_fetch_max、快照不完整，
      调用方必须跳过本轮对比，否则窗口外成员会被误判为加入/退出。
    """
    page_size = 100
    uids: Set[str] = set()
    start = 0
    while start < member_fetch_max:
        result = sender.get_area_members(
            area=area,
            offset_start=start,
            offset_end=start + page_size - 1,
            quiet=True,
        )
        if "error" in result:
            err = str(result.get("error") or "")
            is_rl = err.startswith("HTTP 429") or err in ("invalid JSON", "empty response") or "服务异常" in err
            return None, is_rl, False
        members = result.get("members") or []
        for m in members:
            uid = _member_uid(m)
            if uid:
                uids.add(uid)
        if len(members) < page_size:
            return uids, False, False
        try:
            total = int(result.get("userCount") or result.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        if total and len(uids) >= total:
            return uids, False, False
        start += page_size
    return uids, False, True


def parse_area_operate_log_changes(area: str, payload: dict) -> list[AreaMemberChange]:
    """从域管理日志接口数据中解析成员加入/退出事件。"""
    if not isinstance(payload, dict):
        return []
    logs = payload.get("logs") or []
    if not isinstance(logs, list):
        return []

    changes: list[AreaMemberChange] = []
    for item in logs:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("optUid") or item.get("uid") or item.get("person") or "").strip()
        content = str(item.get("content") or "").strip()
        if not uid or not content:
            continue
        if content == _JOIN_CONTENT:
            action = "join"
        elif content == _LEAVE_CONTENT:
            action = "leave"
        else:
            continue
        try:
            create_time = int(item.get("createTime") or item.get("time") or item.get("timestamp") or 0)
        except (TypeError, ValueError):
            create_time = 0
        changes.append(
            AreaMemberChange(
                action=action,
                area=area,
                uid=uid,
                create_time=create_time,
                content=content,
            )
        )
    return changes


def fetch_operate_log_changes(sender: OopzSender, area: str) -> Tuple[Optional[list[AreaMemberChange]], bool]:
    """拉取一页域管理日志并解析成员变更。"""
    result = sender.get_area_operate_logs(
        area=area,
        offset=0,
        op_types=OPERATE_LOG_MEMBER_OP_TYPES,
    )
    if "error" in result:
        err = str(result.get("error") or "")
        rate_limited = err.startswith("HTTP 429") or "429" in err
        return None, rate_limited
    return parse_area_operate_log_changes(area, result), False


def _build_member_mention(uid: str) -> Tuple[str, list]:
    """构造 Oopz 的 @ 用户正文片段和 mentionList。"""
    uid = (uid or "").strip()
    if not uid:
        return "", []
    return (
        f" {build_mention(uid)}",
        [{
            "person": uid,
            "isBot": False,
            "botType": "",
            "offset": -1,
        }],
    )


def _resolve_role_id(
    sender: OopzSender,
    uid: str,
    area: str,
    auto_role_id: str,
    auto_role_name: str,
) -> Optional[int]:
    """将配置中的 role_id / role_name 解析为数字 role_id。"""
    if auto_role_id:
        try:
            return int(auto_role_id)
        except (ValueError, TypeError):
            logger.warning("auto_assign_role_id 非法: %s", auto_role_id)
            return None
    if not auto_role_name:
        return None
    try:
        roles = sender.get_assignable_roles(uid, area=area)
        for r in roles:
            if str(r.get("name") or "").strip() == auto_role_name.strip():
                return int(r.get("roleID") or r.get("id") or 0) or None
    except Exception as e:
        logger.warning("按名称查找身份组失败 (name=%s): %s", auto_role_name, e)
    return None


def _try_assign_role(
    sender: OopzSender,
    uid: str,
    area: str,
    auto_role_id: str,
    auto_role_name: str,
) -> None:
    """为新成员自动分配身份组，失败仅记录日志。"""
    if not auto_role_id and not auto_role_name:
        return
    role_id = _resolve_role_id(sender, uid, area, auto_role_id, auto_role_name)
    if role_id is None:
        logger.warning("新人身份组分配跳过: 未能解析 role_id (id=%s, name=%s)", auto_role_id, auto_role_name)
        return
    try:
        result = sender.edit_user_role(uid, role_id, add=True, area=area)
        if "error" in result:
            logger.warning("新人身份组分配失败 uid=%s role=%s: %s", uid, role_id, result["error"])
        else:
            logger.info("新人身份组分配成功 uid=%s role=%s", uid, role_id)
    except Exception as e:
        logger.warning("新人身份组分配异常 uid=%s role=%s: %s", uid, role_id, e)


def _run_join_poll_loop(
    sender: OopzSender,
    message_template_join: str,
    interval_seconds: int,
    bot_uid: str,
    auto_role_id: str = "",
    auto_role_name: str = "",
    message_template_leave: str = "",
    on_member_change: Optional[Callable[[str, str, str], None]] = None,
    member_fetch_max: int = 5000,
    event_source: str = EVENT_SOURCE_OPERATE_LOGS,
) -> None:
    """
    后台轮询域成员事件。

    默认使用域管理日志接口解析加入/退出事件；如配置为 member_snapshot，
    则保留旧的成员列表快照对比实现。两种模式不会自动兜底切换。
    """
    from core.area_config import get_area_registry

    last_uids_map: dict[str, Set[str]] = {}
    first_run_set: Set[str] = set()
    truncated_warned: Set[str] = set()
    operate_log_cursor = AreaOperateLogCursor()
    source = event_source if event_source in EVENT_SOURCE_CHOICES else EVENT_SOURCE_OPERATE_LOGS

    min_interval = 5 if source == EVENT_SOURCE_MEMBER_SNAPSHOT else 2
    base_interval = max(min_interval, int(interval_seconds))
    current_interval = base_interval

    def _notify_member_change(action: str, area_id: str, uid: str) -> None:
        if on_member_change is None:
            return
        try:
            on_member_change(action, area_id, uid)
        except Exception as e:
            logger.warning("成员变更回调失败 action=%s area=%s uid=%s: %s", action, area_id[:8], uid[:8], e)

    def _handle_join(area_id: str, channel: str, uid: str, area_cfg) -> None:
        try:
            name = _resolve_display_name(sender, uid, None)
            join_msg = area_cfg.welcome_message if area_cfg.welcome_message else message_template_join
            text = join_msg.format(name=name, uid=uid)
            mention_text, mention_list = _build_member_mention(uid)
            sender.send_message(
                f"{mention_text}\n{text}",
                area=area_id,
                channel=channel,
                auto_recall=False,
                mentionList=mention_list,
            )
        except Exception as e:
            logger.warning("域成员欢迎发送失败 area=%s uid=%s: %s", area_id[:8], uid[:8], e)
        a_role_id = area_cfg.auto_assign_role_id or auto_role_id
        a_role_name = area_cfg.auto_assign_role_name or auto_role_name
        _try_assign_role(sender, uid, area_id, a_role_id, a_role_name)
        _notify_member_change("join", area_id, uid)

    def _handle_leave(area_id: str, channel: str, uid: str, area_cfg) -> None:
        try:
            leave_msg = area_cfg.leave_message if area_cfg.leave_message else message_template_leave
            if leave_msg:
                name = _resolve_display_name(sender, uid, None)
                sender.send_message(
                    leave_msg.format(name=name, uid=uid),
                    area=area_id,
                    channel=channel,
                    auto_recall=False,
                )
        except Exception as e:
            logger.warning("域成员退出通知发送失败 area=%s uid=%s: %s", area_id[:8], uid[:8], e)
        _notify_member_change("leave", area_id, uid)

    def _fetch_member_uids(area: str) -> Tuple[Optional[Set[str]], bool, bool]:
        return fetch_member_uid_snapshot(sender, area, member_fetch_max)

    def _handle_operate_log_area(area_id: str, channel: str, area_cfg) -> bool:
        changes, rate_limited = fetch_operate_log_changes(sender, area_id)
        if changes is None:
            logger.warning("域管理日志轮询失败，跳过本轮 area=%s", area_id[:8])
            return rate_limited
        for change in operate_log_cursor.consume(area_id, changes):
            if not change.uid or change.uid == bot_uid:
                continue
            if change.action == "join":
                _handle_join(area_id, channel, change.uid, area_cfg)
            elif change.action == "leave":
                _handle_leave(area_id, channel, change.uid, area_cfg)
        return False

    def _handle_member_snapshot_area(area_id: str, channel: str, area_cfg) -> bool:
        current_uids, rate_limited, truncated = _fetch_member_uids(area_id)

        if current_uids is None:
            return rate_limited

        if truncated:
            if area_id not in truncated_warned:
                truncated_warned.add(area_id)
                logger.warning(
                    "域 %s 成员数量超过 member_fetch_max=%d，成员快照不完整，已暂停该域的加入/退出检测",
                    area_id[:8],
                    member_fetch_max,
                )
            return False

        is_first = area_id not in first_run_set
        if is_first:
            last_uids_map[area_id] = current_uids
            first_run_set.add(area_id)
            return False

        prev = last_uids_map.get(area_id, set())
        new_uids = current_uids - prev
        left_uids = prev - current_uids
        last_uids_map[area_id] = current_uids
        for uid in new_uids:
            if not uid or uid == bot_uid:
                continue
            _handle_join(area_id, channel, uid, area_cfg)
        for uid in left_uids:
            if not uid or uid == bot_uid:
                continue
            _handle_leave(area_id, channel, uid, area_cfg)
        return False

    def _resolve_area_channel(area_id: str) -> Tuple[str, str]:
        registry = get_area_registry()
        ch = registry.get_default_channel(area_id)
        if ch:
            return area_id, ch
        return _get_default_area_channel(sender, quiet=True)

    def _get_poll_areas() -> list[str]:
        registry = get_area_registry()
        area_ids = registry.get_all_area_ids()
        if area_ids:
            return area_ids
        a, _ = _get_default_area_channel(sender, quiet=True)
        return [a] if a else []

    while True:
        try:
            poll_areas = _get_poll_areas()
            if not poll_areas:
                if not last_uids_map:
                    logger.warning("域成员加入轮询: 未获取到任何域，请配置 AREA_CONFIGS 或 default_area")
                time.sleep(current_interval)
                continue

            registry = get_area_registry()
            any_rate_limited = False

            for area in poll_areas:
                area_channel = _resolve_area_channel(area)
                area_id, channel = area_channel
                if not area_id or not channel:
                    continue

                area_cfg = registry.get(area_id)
                if source == EVENT_SOURCE_MEMBER_SNAPSHOT:
                    rate_limited = _handle_member_snapshot_area(area_id, channel, area_cfg)
                else:
                    rate_limited = _handle_operate_log_area(area_id, channel, area_cfg)
                any_rate_limited = any_rate_limited or rate_limited

            if any_rate_limited:
                next_interval = _next_poll_interval(base_interval, current_interval, True)
                if next_interval != current_interval:
                    logger.warning("域成员加入轮询: 检测到限流，轮询间隔调整为 %ss", next_interval)
                current_interval = next_interval
            elif current_interval != base_interval:
                current_interval = base_interval
                logger.info("域成员加入轮询: 成员接口已恢复，轮询间隔恢复为 %ss", current_interval)

            time.sleep(current_interval)
        except Exception as e:
            logger.warning("域成员加入轮询异常: %s", e)
            time.sleep(current_interval)


def make_ws_handler(
    sender: OopzSender,
    message_template_join: str,
    message_template_leave: str,
) -> Callable[[int, dict], None]:
    from core.area_config import get_area_registry

    bot_uid = (OOPZ_CONFIG.get("person_uid") or "").strip()
    _channel_cache: dict[str, str] = {}

    def _resolve_channel_for_area(area: str) -> str:
        if area in _channel_cache:
            return _channel_cache[area]
        registry = get_area_registry()
        ch = registry.get_default_channel(area)
        if ch:
            _channel_cache[area] = ch
            return ch
        for g in sender.get_area_channels(area=area, quiet=True):
            for c in (g.get("channels") or []):
                if (c.get("type") or "").upper() != "VOICE":
                    ch_id = (c.get("id") or "").strip()
                    if ch_id:
                        _channel_cache[area] = ch_id
                        return ch_id
        return ""

    _IGNORE_EVENTS = (0,)

    def _on_other_event(event: int, data: dict):
        parsed = _parse_member_event(event, data)
        if not parsed:
            if event in _IGNORE_EVENTS:
                return
            return
        action, area, uid = parsed
        if uid == bot_uid:
            return

        registry = get_area_registry()
        configured_areas = registry.get_all_area_ids()
        if configured_areas and area not in configured_areas:
            return

        ch = _resolve_channel_for_area(area)
        if not ch:
            logger.warning("域成员通知跳过: 域 %s 未获取到默认频道", area[:8])
            return

        area_cfg = registry.get(area)
        try:
            name = _resolve_display_name(sender, uid, None)
            if action == "join":
                join_msg = area_cfg.welcome_message or message_template_join
                text = join_msg.format(name=name, uid=uid)
                mention_text, mention_list = _build_member_mention(uid)
                sender.send_message(
                    f"{mention_text}\n{text}",
                    area=area,
                    channel=ch,
                    auto_recall=False,
                    mentionList=mention_list,
                )
            else:
                leave_msg = area_cfg.leave_message or message_template_leave
                text = leave_msg.format(name=name, uid=uid)
                sender.send_message(text, area=area, channel=ch, auto_recall=False)
        except Exception as e:
            logger.warning("域成员通知发送失败: %s", e)

    return _on_other_event


def start_area_join_notifier(
    sender: Optional[OopzSender] = None,
    message_template_join: str = "欢迎 {name} 加入域～",
    message_template_leave: str = "{name} 已退出域",
    on_member_change: Optional[Callable[[str, str, str], None]] = None,
) -> Optional[Callable[[int, dict], None]]:
    try:
        import config as _config
        config = getattr(_config, "AREA_JOIN_NOTIFY", None)
    except Exception:
        config = None

    if not config or not config.get("enabled", False):
        return None

    msg_join = str(config.get("message_template", message_template_join) or message_template_join)
    if "{name}" not in msg_join and "{uid}" not in msg_join:
        msg_join = "欢迎 {name} 加入域～"
    # 显式留空（"" 或 None）= 不在频道发退出提示，但 OneBot group_decrease 仍会推送。
    raw_leave = config.get("message_template_leave", message_template_leave)
    if raw_leave is None or (isinstance(raw_leave, str) and not raw_leave.strip()):
        msg_leave = ""
    else:
        msg_leave = str(raw_leave)
        if "{name}" not in msg_leave and "{uid}" not in msg_leave:
            msg_leave = "{name} 已退出域"

    s = sender or OopzSender()
    # 加入事件服务端不推送，用轮询检测新成员并发欢迎
    poll_interval = max(5, int(config.get("poll_interval_seconds", 10)))
    bot_uid = (OOPZ_CONFIG.get("person_uid") or "").strip()
    auto_role_id = str(config.get("auto_assign_role_id") or "").strip()
    auto_role_name = str(config.get("auto_assign_role_name") or "").strip()
    try:
        member_fetch_max = max(200, int(config.get("member_fetch_max", 5000)))
    except (TypeError, ValueError):
        member_fetch_max = 5000
    event_source = str(config.get("event_source") or EVENT_SOURCE_OPERATE_LOGS).strip()
    if event_source not in EVENT_SOURCE_CHOICES:
        logger.warning("AREA_JOIN_NOTIFY.event_source=%s 无效，使用 %s", event_source, EVENT_SOURCE_OPERATE_LOGS)
        event_source = EVENT_SOURCE_OPERATE_LOGS
    if auto_role_id or auto_role_name:
        logger.info("新人自动身份组已启用: id=%s, name=%s", auto_role_id or "(无)", auto_role_name or "(无)")
    poll_thread = threading.Thread(
        target=_run_join_poll_loop,
        args=(s, msg_join, poll_interval, bot_uid, auto_role_id, auto_role_name, msg_leave, on_member_change),
        kwargs={"member_fetch_max": member_fetch_max, "event_source": event_source},
        daemon=True,
        name="AreaJoinPoll",
    )
    poll_thread.start()
    return make_ws_handler(s, msg_join, msg_leave)
