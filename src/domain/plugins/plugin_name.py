import re

PLUGIN_NAME_PATTERN = re.compile(r"[A-Za-z0-9_]+")


def normalize_plugin_name(raw_name: str) -> str | None:
    name = (raw_name or "").strip()
    if not PLUGIN_NAME_PATTERN.fullmatch(name):
        return None
    return name
