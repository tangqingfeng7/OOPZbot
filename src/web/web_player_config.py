"""Web 播放器配置管理 — 配置分组、校验、运行时辅助函数。"""

from __future__ import annotations

import ast
import copy
import importlib
import json
import os

import config as runtime_config
from core.logger_config import get_logger

logger = get_logger("WebPlayerConfig")

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

from core.paths import PROJECT_ROOT  # noqa: E402 — 统一根路径来源，re-export 供 cfg.PROJECT_ROOT 使用
from core.redis_keys import ADMIN_SESSION as KEY_ADMIN_SESSION, ADMIN_SESSION_COOKIE  # noqa: E402
CONFIG_FILE_PATH = os.path.join(PROJECT_ROOT, "config.py")

# 分组名 → config.py 中的源变量名
CONFIG_GROUP_SOURCES: dict[str, str] = {
    "web_player": "WEB_PLAYER_CONFIG",
    "auto_recall": "AUTO_RECALL_CONFIG",
    "area_join_notify": "AREA_JOIN_NOTIFY",
    "chat": "CHAT_CONFIG",
    "profanity": "PROFANITY_CONFIG",
    "oopz": "OOPZ_CONFIG",
    "netease": "NETEASE_CLOUD",
    "redis": "REDIS_CONFIG",
    "doubao_chat": "DOUBAO_CONFIG",
    "doubao_image": "DOUBAO_IMAGE_CONFIG",
    "scheduler": "SCHEDULER_CONFIG",
    "reminder": "REMINDER_CONFIG",
    "music": "MUSIC_CONFIG",
    "command_cooldown": "COMMAND_COOLDOWN_CONFIG",
    "qq_music": "QQ_MUSIC_CONFIG",
    "bilibili_music": "BILIBILI_MUSIC_CONFIG",
    "message_stats": "MESSAGE_STATS_CONFIG",
}

# ---------------------------------------------------------------------------
# 配置字段规范（CONFIG_FIELD_SCHEMA）—— 配置默认值与校验规则的【单一来源】。
#
# 每个字段的 type/min/max/max_len/sensitive 以及 default 都只在此声明一次：
#   - 运行时兜底默认（config.py 缺该分组时）由此派生；
#   - 后台保存的类型转换 / 范围校验读取此处；
#   - 前端表单的字段类型与默认值通过 config_field_schema() 下发，不再在 JS / HTML 里另写一份。
# default 必须与 config.example.py 中对应分组的取值保持一致。
# ---------------------------------------------------------------------------

CONFIG_FIELD_SCHEMA: dict[str, dict[str, dict]] = {
    "web_player": {
        "url": {"type": "str", "max_len": 300, "default": ""},
        "host": {"type": "str", "max_len": 64, "default": "0.0.0.0"},
        "port": {"type": "int", "min": 1, "max": 65535, "default": 8080},
        "token_ttl_seconds": {"type": "int", "min": 0, "max": 7 * 24 * 3600, "default": 86400},
        "cookie_max_age_seconds": {"type": "int", "min": 0, "max": 30 * 24 * 3600, "default": 86400},
        "cookie_secure": {"type": "bool", "default": False},
        "send_link_enabled": {"type": "bool", "default": True},
        "link_idle_release_seconds": {"type": "int", "min": 0, "max": 30 * 24 * 3600, "default": 1800},
        "admin_enabled": {"type": "bool", "default": False},
        "admin_password": {"type": "str", "max_len": 128, "sensitive": True, "default": ""},
        "admin_session_ttl_seconds": {"type": "int", "min": 0, "max": 30 * 24 * 3600, "default": 43200},
        "admin_cookie_secure": {"type": "bool", "default": False},
        "admin_login_max_failures": {"type": "int", "min": 0, "max": 100, "default": 5},
        "admin_login_lock_seconds": {"type": "int", "min": 0, "max": 24 * 3600, "default": 300},
        "trust_proxy_header": {"type": "bool", "default": True},
    },
    "auto_recall": {
        "enabled": {"type": "bool", "default": False},
        "delay": {"type": "int", "min": 1, "max": 3600, "default": 30},
        "exclude_commands": {"type": "str_list", "max_len": 500, "default": ["ai_chat", "ai_image"]},
    },
    "area_join_notify": {
        "enabled": {"type": "bool", "default": False},
        "event_source": {"type": "str", "max_len": 32, "default": "operate_logs"},
        "message_template": {"type": "str", "max_len": 200, "default": "欢迎 {name} 加入域～\n请阅读频道规则，祝你玩得开心！"},
        "message_template_leave": {"type": "str", "max_len": 200, "default": "{name} 已退出域"},
        "poll_interval_seconds": {"type": "int", "min": 2, "max": 3600, "default": 2},
        "auto_assign_role_id": {"type": "str", "max_len": 128, "default": ""},
        "auto_assign_role_name": {"type": "str", "max_len": 128, "default": ""},
        "member_fetch_max": {"type": "int", "min": 200, "max": 100000, "default": 5000},
    },
    "chat": {
        "enabled": {"type": "bool", "default": True},
        "keyword_replies": {
            "type": "json_dict",
            "max_len": 5000,
            "default": {
                "你好": "你好呀！我是 Oopz Bot ~",
                "帮助": "输入 /help 查看可用命令",
                "ping": "pong!",
            },
        },
    },
    "profanity": {
        "enabled": {"type": "bool", "default": True},
        "mute_duration": {"type": "int", "min": 1, "max": 10080, "default": 5},
        "recall_message": {"type": "bool", "default": True},
        "skip_admins": {"type": "bool", "default": True},
        "warn_before_mute": {"type": "bool", "default": False},
        "context_detection": {"type": "bool", "default": True},
        "context_window": {"type": "int", "min": 5, "max": 300, "default": 30},
        "context_max_messages": {"type": "int", "min": 1, "max": 50, "default": 10},
        "ai_detection": {"type": "bool", "default": True},
        "ai_min_length": {"type": "int", "min": 1, "max": 50, "default": 2},
    },
    "oopz": {
        "login_phone": {"type": "str", "max_len": 128, "sensitive": True, "expose_in_admin": True, "default": ""},
        "login_password": {"type": "str", "max_len": 256, "sensitive": True, "default": ""},
        "default_area": {"type": "str", "max_len": 128, "default": ""},
        "default_channel": {"type": "str", "max_len": 128, "default": ""},
        "proxy": {"type": "str", "max_len": 300, "default": ""},
        "agora_app_id": {"type": "str", "max_len": 128, "default": "358eebceadb94c2a9fd91ecd7b341602"},
        "agora_init_timeout": {"type": "int", "min": 10, "max": 7200, "default": 1800},
    },
    "netease": {
        "base_url": {"type": "str", "max_len": 300, "default": "http://localhost:3000"},
        "cookie": {"type": "str", "max_len": 6000, "sensitive": True, "expose_in_admin": True, "default": ""},
        "audio_download_timeout": {"type": "int", "min": 5, "max": 600, "default": 120},
        "audio_download_retries": {"type": "int", "min": 0, "max": 10, "default": 2},
        "audio_quality": {"type": "str", "max_len": 20, "default": "standard"},
    },
    "redis": {
        "host": {"type": "str", "max_len": 200, "default": "127.0.0.1"},
        "port": {"type": "int", "min": 1, "max": 65535, "default": 6379},
        "password": {"type": "str", "max_len": 256, "sensitive": True, "default": ""},
        "db": {"type": "int", "min": 0, "max": 15, "default": 0},
        "decode_responses": {"type": "bool", "default": True},
    },
    "doubao_chat": {
        "enabled": {"type": "bool", "default": False},
        "base_url": {"type": "str", "max_len": 300, "default": "https://ark.cn-beijing.volces.com/api/v3"},
        "api_key": {"type": "str", "max_len": 256, "sensitive": True, "expose_in_admin": True, "default": ""},
        "model": {"type": "str", "max_len": 120, "default": "doubao-1-5-pro-32k-250115"},
        "system_prompt": {"type": "str", "max_len": 5000, "default": "你是 Oopz Bot，一个活泼有趣的聊天机器人。回复简洁友好，不超过100字。"},
        "max_tokens": {"type": "int", "min": 1, "max": 8192, "default": 256},
        "temperature": {"type": "float", "min": 0, "max": 2, "default": 0.7},
        "context_max_rounds": {"type": "int", "min": 0, "max": 50, "default": 10},
        "context_ttl_seconds": {"type": "int", "min": 0, "max": 86400, "default": 1800},
    },
    "doubao_image": {
        "enabled": {"type": "bool", "default": False},
        "base_url": {"type": "str", "max_len": 300, "default": "https://ark.cn-beijing.volces.com/api/v3"},
        "api_key": {"type": "str", "max_len": 256, "sensitive": True, "expose_in_admin": True, "default": ""},
        "model": {"type": "str", "max_len": 120, "default": "doubao-seedream-4-5-251128"},
        "size": {"type": "str", "max_len": 30, "default": "1920x1920"},
        "watermark": {"type": "bool", "default": False},
    },
    "scheduler": {
        "enabled": {"type": "bool", "default": True},
        "check_interval_seconds": {"type": "int", "min": 10, "max": 3600, "default": 30},
    },
    "reminder": {
        "enabled": {"type": "bool", "default": True},
        "max_per_user": {"type": "int", "min": 1, "max": 100, "default": 5},
        "max_delay_hours": {"type": "int", "min": 1, "max": 720, "default": 72},
        "check_interval_seconds": {"type": "int", "min": 5, "max": 3600, "default": 15},
    },
    "music": {
        "auto_play_enabled": {"type": "bool", "default": True},
        "default_volume": {"type": "int", "min": 0, "max": 100, "default": 50},
    },
    "command_cooldown": {
        "enabled": {"type": "bool", "default": False},
        "default_seconds": {"type": "int", "min": 0, "max": 300, "default": 3},
        "exempt_admins": {"type": "bool", "default": True},
    },
    "qq_music": {
        "enabled": {"type": "bool", "default": False},
        "base_url": {"type": "str", "max_len": 300, "default": "http://localhost:3300"},
        "cookie": {"type": "str", "max_len": 3000, "sensitive": True, "expose_in_admin": True, "default": ""},
    },
    "bilibili_music": {
        "enabled": {"type": "bool", "default": False},
        "cookie": {"type": "str", "max_len": 3000, "sensitive": True, "expose_in_admin": True, "default": ""},
    },
    "message_stats": {
        "enabled": {"type": "bool", "default": True},
    },
}

# 由 schema 派生的「每分组默认值字典」—— 运行时兜底与默认值查询的唯一出处。
GROUP_DEFAULTS: dict[str, dict] = {
    group: {
        field: copy.deepcopy(meta["default"])
        for field, meta in fields.items()
        if "default" in meta
    }
    for group, fields in CONFIG_FIELD_SCHEMA.items()
}


def group_defaults(group: str) -> dict:
    """返回某分组的默认值字典副本（schema 派生）。"""
    return copy.deepcopy(GROUP_DEFAULTS.get(group, {}))


def config_default(group: str, field: str):
    """读取单个字段的默认值（schema 派生）。"""
    return GROUP_DEFAULTS[group][field]


def _resolve_group_target(group: str) -> dict:
    """从 config.py 读取分组配置；缺失时回退到 schema 派生的默认值。"""
    value = getattr(runtime_config, CONFIG_GROUP_SOURCES[group], None)
    if isinstance(value, dict):
        return value
    return group_defaults(group)


# ---------------------------------------------------------------------------
# 运行时配置引用（与 config.py 中的 dict 为同一对象，支持就地热更新）
# ---------------------------------------------------------------------------

_GROUP_TARGETS: dict[str, dict] = {group: _resolve_group_target(group) for group in CONFIG_FIELD_SCHEMA}

WEB_PLAYER_CONFIG = _GROUP_TARGETS["web_player"]
AUTO_RECALL_CONFIG = _GROUP_TARGETS["auto_recall"]
AREA_JOIN_NOTIFY = _GROUP_TARGETS["area_join_notify"]
CHAT_CONFIG = _GROUP_TARGETS["chat"]
PROFANITY_CONFIG = _GROUP_TARGETS["profanity"]
OOPZ_CONFIG = _GROUP_TARGETS["oopz"]
NETEASE_CLOUD = _GROUP_TARGETS["netease"]
REDIS_CONFIG = _GROUP_TARGETS["redis"]
DOUBAO_CONFIG = _GROUP_TARGETS["doubao_chat"]
DOUBAO_IMAGE_CONFIG = _GROUP_TARGETS["doubao_image"]
SCHEDULER_CONFIG = _GROUP_TARGETS["scheduler"]
REMINDER_CONFIG = _GROUP_TARGETS["reminder"]
MUSIC_CONFIG = _GROUP_TARGETS["music"]
COMMAND_COOLDOWN_CONFIG = _GROUP_TARGETS["command_cooldown"]
QQ_MUSIC_CONFIG = _GROUP_TARGETS["qq_music"]
BILIBILI_MUSIC_CONFIG = _GROUP_TARGETS["bilibili_music"]
MESSAGE_STATS_CONFIG = _GROUP_TARGETS["message_stats"]

# ---------------------------------------------------------------------------
# 配置分组定义（target=运行时 dict，fields=schema；二者均派生自上方单一来源）
# ---------------------------------------------------------------------------

CONFIG_GROUPS: dict[str, dict] = {
    group: {"target": _GROUP_TARGETS[group], "fields": CONFIG_FIELD_SCHEMA[group]}
    for group in CONFIG_FIELD_SCHEMA
}

CONFIG_BASELINES: dict[str, dict] = {
    group: copy.deepcopy(CONFIG_GROUPS[group]["target"])
    for group in CONFIG_GROUPS
    if isinstance(CONFIG_GROUPS[group].get("target"), dict)
}


def config_field_schema() -> dict:
    """供前端表单下发的字段规范：每字段的 type / default / 数值边界。

    敏感字段不下发 default（其默认即空串，且明文由 config_snapshot 单独控制）。
    """
    schema: dict[str, dict] = {}
    for group, fields in CONFIG_FIELD_SCHEMA.items():
        section: dict[str, dict] = {}
        for field, meta in fields.items():
            entry: dict = {"type": meta["type"]}
            if not meta.get("sensitive") and "default" in meta:
                entry["default"] = copy.deepcopy(meta["default"])
            for bound in ("min", "max"):
                if bound in meta:
                    entry[bound] = meta[bound]
            section[field] = entry
        schema[group] = section
    return schema

# ---------------------------------------------------------------------------
# 配置辅助函数
# ---------------------------------------------------------------------------

_DEFAULT_COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 days


def token_ttl_seconds() -> int:
    default = config_default("web_player", "token_ttl_seconds")
    try:
        ttl = int(WEB_PLAYER_CONFIG.get("token_ttl_seconds", default) or 0)
    except (TypeError, ValueError):
        ttl = default
    return ttl if ttl > 0 else 0


def cookie_max_age_seconds() -> int:
    configured = WEB_PLAYER_CONFIG.get("cookie_max_age_seconds")
    if configured is not None:
        try:
            v = int(configured)
            return v if v > 0 else _DEFAULT_COOKIE_MAX_AGE
        except (TypeError, ValueError):
            pass
    ttl = token_ttl_seconds()
    return ttl if ttl > 0 else _DEFAULT_COOKIE_MAX_AGE


def cookie_secure() -> bool:
    return bool(WEB_PLAYER_CONFIG.get("cookie_secure", False))


def admin_enabled() -> bool:
    return bool(WEB_PLAYER_CONFIG.get("admin_enabled", False))


def admin_password() -> str:
    value = WEB_PLAYER_CONFIG.get("admin_password", "")
    return str(value).strip() if value is not None else ""


def admin_session_ttl_seconds() -> int:
    default = config_default("web_player", "admin_session_ttl_seconds")
    try:
        ttl = int(WEB_PLAYER_CONFIG.get("admin_session_ttl_seconds", default) or 0)
    except (TypeError, ValueError):
        ttl = default
    return ttl if ttl > 0 else 0


def admin_cookie_secure() -> bool:
    return bool(WEB_PLAYER_CONFIG.get("admin_cookie_secure", cookie_secure()))


def admin_login_max_failures() -> int:
    default = config_default("web_player", "admin_login_max_failures")
    try:
        return max(0, int(WEB_PLAYER_CONFIG.get("admin_login_max_failures", default)))
    except (TypeError, ValueError):
        return default


def admin_login_lock_seconds() -> int:
    default = config_default("web_player", "admin_login_lock_seconds")
    try:
        return max(0, int(WEB_PLAYER_CONFIG.get("admin_login_lock_seconds", default)))
    except (TypeError, ValueError):
        return default


def trust_proxy_header() -> bool:
    """是否信任反代透传的 X-Real-IP / X-Forwarded-For 来判定客户端 IP。

    默认信任（文档推荐的部署形态就是 nginx 反代）。把 uvicorn 直接暴露到公网时
    应设为 False，否则任何人都能伪造这两个头绕过限流与登录锁定。
    """
    return bool(WEB_PLAYER_CONFIG.get("trust_proxy_header", True))


def admin_cookie_name() -> str:
    return ADMIN_SESSION_COOKIE


def default_music_volume() -> int:
    default = config_default("music", "default_volume")
    try:
        volume = int(MUSIC_CONFIG.get("default_volume", default))
    except (TypeError, ValueError):
        volume = default
    return max(0, min(100, volume))


def web_host() -> str:
    """Web 播放器监听地址。"""
    return str(WEB_PLAYER_CONFIG.get("host", config_default("web_player", "host")))


def web_port() -> int:
    """Web 播放器监听端口。"""
    return int(WEB_PLAYER_CONFIG.get("port", config_default("web_player", "port")))


# ---------------------------------------------------------------------------
# 类型强转 / 校验
# ---------------------------------------------------------------------------

def to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            return True
        if v in {"0", "false", "no", "off"}:
            return False
    raise ValueError("布尔值格式无效")


def coerce_config_value(meta: dict, raw: object) -> object:
    value_type = meta.get("type")
    if value_type == "bool":
        return to_bool(raw)
    if value_type == "float":
        try:
            v = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError("浮点数格式无效")
        min_v = meta.get("min")
        max_v = meta.get("max")
        if min_v is not None and v < min_v:
            raise ValueError(f"必须 >= {min_v}")
        if max_v is not None and v > max_v:
            raise ValueError(f"必须 <= {max_v}")
        return v
    if value_type == "int":
        try:
            v = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError("整数格式无效")
        min_v = meta.get("min")
        max_v = meta.get("max")
        if min_v is not None and v < min_v:
            raise ValueError(f"必须 >= {min_v}")
        if max_v is not None and v > max_v:
            raise ValueError(f"必须 <= {max_v}")
        return v
    if value_type == "str":
        text = "" if raw is None else str(raw)
        max_len = meta.get("max_len")
        if max_len is not None and len(text) > max_len:
            raise ValueError(f"长度不能超过 {max_len}")
        return text
    if value_type == "str_list":
        if isinstance(raw, str):
            items = [s.strip() for s in raw.split(",") if s.strip()]
        elif isinstance(raw, list):
            items = [str(s).strip() for s in raw if str(s).strip()]
        else:
            raise ValueError("需要字符串列表或逗号分隔的字符串")
        max_len = meta.get("max_len")
        joined = ",".join(items)
        if max_len is not None and len(joined) > max_len:
            raise ValueError(f"总长度不能超过 {max_len}")
        return items
    if value_type == "json_dict":
        if isinstance(raw, dict):
            d = raw
        elif isinstance(raw, str):
            try:
                d = json.loads(raw)
            except Exception:
                raise ValueError("JSON 格式无效")
            if not isinstance(d, dict):
                raise ValueError("必须是 JSON 对象")
        else:
            raise ValueError("需要 JSON 对象或字符串")
        max_len = meta.get("max_len")
        serialized = json.dumps(d, ensure_ascii=False)
        if max_len is not None and len(serialized) > max_len:
            raise ValueError(f"总长度不能超过 {max_len}")
        return d
    raise ValueError(f"未知类型: {value_type}")


# ---------------------------------------------------------------------------
# 管理后台配置更新（先改当前进程，再写回 config.py）
# ---------------------------------------------------------------------------


def apply_config_updates(updates: dict) -> tuple[dict, list[str], dict]:
    applied: dict = {}
    errors: list[str] = []
    persist_payload: dict = {}
    for group_name, patch in (updates or {}).items():
        group = CONFIG_GROUPS.get(group_name)
        if not group:
            errors.append(f"未知配置分组: {group_name}")
            continue
        if not isinstance(patch, dict):
            errors.append(f"配置分组 {group_name} 必须是对象")
            continue
        target = group.get("target")
        fields = group.get("fields", {})
        if not isinstance(target, dict):
            errors.append(f"配置分组 {group_name} 不可写")
            continue
        for field, raw in patch.items():
            meta = fields.get(field)
            if not meta:
                errors.append(f"配置项不允许修改: {group_name}.{field}")
                continue
            if meta.get("sensitive") and (raw is None or str(raw).strip() == ""):
                continue
            try:
                value = coerce_config_value(meta, raw)
            except Exception as e:
                errors.append(f"配置项 {group_name}.{field} 校验失败: {e}")
                continue
            target[field] = value
            applied.setdefault(group_name, {})
            persist_payload.setdefault(group_name, {})
            if meta.get("sensitive"):
                applied[group_name][field] = "***"
            else:
                applied[group_name][field] = value
            persist_payload[group_name][field] = value
    return applied, errors, persist_payload


def _python_literal(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_python_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = [
            f"{_python_literal(str(key))}: {_python_literal(item)}"
            for key, item in value.items()
        ]
        return "{" + ", ".join(parts) + "}"
    return json.dumps(str(value), ensure_ascii=False)


def _line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    return offsets


def _byte_col_to_char_index(line: str, byte_col: int) -> int:
    total = 0
    for index, char in enumerate(line):
        total += len(char.encode("utf-8"))
        if total > byte_col:
            return index
        if total == byte_col:
            return index + 1
    return len(line)


def _node_span(lines: list[str], offsets: list[int], node: ast.AST) -> tuple[int, int]:
    start_col = _byte_col_to_char_index(lines[node.lineno - 1], node.col_offset)
    end_col = _byte_col_to_char_index(lines[node.end_lineno - 1], node.end_col_offset)
    start = offsets[node.lineno - 1] + start_col
    end = offsets[node.end_lineno - 1] + end_col
    return start, end


def _dict_assignments(tree: ast.AST) -> dict[str, ast.Dict]:
    assignments: dict[str, ast.Dict] = {}
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                assignments[target.id] = node.value
    return assignments


def _format_config_assignment(source_name: str, values: dict) -> str:
    lines = ["\n\n", f"{source_name} = {{\n"]
    for key, value in values.items():
        lines.append(f"    {_python_literal(str(key))}: {_python_literal(value)},\n")
    lines.append("}\n")
    return "".join(lines)


def persist_config_updates(updates: dict, path: str | None = None) -> None:
    """把后台保存的配置写回 config.py；内存生效由 apply_config_updates 负责。"""
    if not updates:
        return
    config_path = path or CONFIG_FILE_PATH
    if not os.path.exists(config_path):
        raise RuntimeError("config.py 不存在，无法保存配置")

    with open(config_path, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    try:
        tree = ast.parse(text, filename=config_path)
    except SyntaxError as exc:
        raise RuntimeError(f"config.py 语法错误，无法保存配置: {exc}") from exc

    lines = text.splitlines(keepends=True)
    offsets = _line_offsets(lines)
    assignments = _dict_assignments(tree)
    replacements: list[tuple[int, int, str]] = []
    insertions: dict[int, list[str]] = {}

    for group_name, values in (updates or {}).items():
        if not isinstance(values, dict) or not values:
            continue
        source_name = CONFIG_GROUP_SOURCES.get(group_name)
        if not source_name:
            continue
        dict_node = assignments.get(source_name)
        if dict_node is None:
            new_values = copy.deepcopy(CONFIG_BASELINES.get(group_name, {}))
            new_values.update(values)
            insertions.setdefault(len(text), []).append(
                _format_config_assignment(source_name, new_values)
            )
            continue

        existing_keys: dict[str, ast.AST] = {}
        for key_node, value_node in zip(dict_node.keys, dict_node.values):
            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                existing_keys[key_node.value] = value_node

        for field, value in values.items():
            literal = _python_literal(value)
            value_node = existing_keys.get(field)
            if value_node is not None:
                start, end = _node_span(lines, offsets, value_node)
                replacements.append((start, end, literal))
                continue

            closing_line = lines[dict_node.end_lineno - 1]
            closing_indent = closing_line[: len(closing_line) - len(closing_line.lstrip())]
            entry_indent = closing_indent + "    "
            insert_pos = offsets[dict_node.end_lineno - 1]
            insertions.setdefault(insert_pos, []).append(
                f"{entry_indent}{_python_literal(field)}: {literal},\n"
            )

    edits: list[tuple[int, int, str]] = replacements[:]
    for pos, chunks in insertions.items():
        edits.append((pos, pos, "".join(chunks)))
    if not edits:
        return

    for start, end, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
        text = text[:start] + replacement + text[end:]

    temp_path = f"{config_path}.tmp"
    with open(temp_path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(temp_path, config_path)


def persist_admin_uids(uids: list, path: str | None = None) -> None:
    config_path = path or CONFIG_FILE_PATH
    if not os.path.exists(config_path):
        raise RuntimeError("config.py 不存在，无法保存管理员列表")

    with open(config_path, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    try:
        tree = ast.parse(text, filename=config_path)
    except SyntaxError as exc:
        raise RuntimeError(f"config.py 语法错误，无法保存管理员列表: {exc}") from exc

    lines = text.splitlines(keepends=True)
    offsets = _line_offsets(lines)
    replacement = _python_literal([str(uid) for uid in uids])
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "ADMIN_UIDS" for target in node.targets):
            continue
        start, end = _node_span(lines, offsets, node.value)
        text = text[:start] + replacement + text[end:]
        temp_path = f"{config_path}.tmp"
        with open(temp_path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(temp_path, config_path)
        return
    raise RuntimeError("config.py 中找不到 ADMIN_UIDS")


def reload_config_from_file() -> None:
    """重新读取 config.py，并把值同步到现有字典引用，避免重启。"""
    old_admin_uids = getattr(runtime_config, "ADMIN_UIDS", None)
    importlib.invalidate_caches()
    importlib.reload(runtime_config)
    for group_name, source_name in CONFIG_GROUP_SOURCES.items():
        group = CONFIG_GROUPS.get(group_name)
        if not group:
            continue
        target = group.get("target")
        fresh = getattr(runtime_config, source_name, None)
        if isinstance(target, dict) and isinstance(fresh, dict):
            target.clear()
            target.update(copy.deepcopy(fresh))
            setattr(runtime_config, source_name, target)
    fresh_admin_uids = getattr(runtime_config, "ADMIN_UIDS", None)
    if isinstance(old_admin_uids, list) and isinstance(fresh_admin_uids, list):
        old_admin_uids.clear()
        old_admin_uids.extend(str(uid) for uid in fresh_admin_uids)
        setattr(runtime_config, "ADMIN_UIDS", old_admin_uids)


def config_snapshot() -> dict:
    result: dict = {}
    for group_name, group in CONFIG_GROUPS.items():
        target = group.get("target")
        fields = group.get("fields", {})
        if not isinstance(target, dict):
            continue
        section: dict = {}
        for field, meta in fields.items():
            if meta.get("sensitive"):
                value = target.get(field, "")
                if group_name == "oopz" and field == "login_phone" and not value:
                    value = target.get("phone", "")
                if group_name == "oopz" and field == "login_password" and not value:
                    value = target.get("password", "")
                section[field] = value if meta.get("expose_in_admin") else ""
                section[f"{field}_configured"] = bool(value)
            else:
                section[field] = target.get(field, copy.deepcopy(meta.get("default")))
        result[group_name] = section
    return result


_refresh_callbacks: list = []


def on_config_refresh(callback) -> None:
    """注册配置变更后的回调，避免直接导入产生循环依赖。"""
    _refresh_callbacks.append(callback)


def refresh_runtime_dependents(applied_groups: set[str]) -> None:
    if "redis" not in applied_groups and "web_player" not in applied_groups:
        return
    for cb in _refresh_callbacks:
        try:
            cb()
        except Exception as e:
            logger.debug("Config refresh callback failed: %s", e)


def display_web_base_url() -> str:
    configured = str(WEB_PLAYER_CONFIG.get("url", "") or "").strip()
    if configured:
        return configured.rstrip("/")
    host = str(WEB_PLAYER_CONFIG.get("host", "127.0.0.1") or "127.0.0.1").strip()
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = WEB_PLAYER_CONFIG.get("port") or config_default("web_player", "port")
    return f"http://{host}:{port}"


# ---------------------------------------------------------------------------
# 会话管理辅助
# ---------------------------------------------------------------------------

def admin_session_key(token: str) -> str:
    return f"{KEY_ADMIN_SESSION}:{token}"


# ---------------------------------------------------------------------------
# 域配置持久化 (area_configs)
# ---------------------------------------------------------------------------

AREA_OVERRIDES_PATH = os.path.join(PROJECT_ROOT, "data", "area_configs_override.json")


def read_area_overrides() -> dict:
    if not os.path.exists(AREA_OVERRIDES_PATH):
        return {}
    try:
        with open(AREA_OVERRIDES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("读取域配置覆盖文件失败: %s", e)
        return {}


def write_area_overrides(payload: dict) -> None:
    os.makedirs(os.path.dirname(AREA_OVERRIDES_PATH), exist_ok=True)
    with open(AREA_OVERRIDES_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def bootstrap_area_overrides() -> None:
    """启动时加载域配置覆盖到 AreaConfigRegistry。"""
    saved = read_area_overrides()
    if not saved:
        return
    try:
        from core.area_config import get_area_registry
        reg = get_area_registry()
        loaded = 0
        for area_id, raw in saved.items():
            if isinstance(raw, dict):
                reg.update_config(area_id, raw)
                loaded += 1
        if loaded:
            logger.info("从覆盖文件恢复了 %d 个域配置", loaded)
    except Exception as e:
        logger.warning("恢复域配置覆盖失败: %s", e)
