from __future__ import annotations

import importlib.util
import os
import sys
from types import ModuleType
from typing import Any

from domain.plugins.base import BotModule


PROJECT_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))
            )
        )
    )
)
SRC_DIR = os.path.join(PROJECT_ROOT, "src")


def ensure_src_on_path(project_root: str = PROJECT_ROOT) -> None:
    """确保 `src/` 已加入导入路径。"""
    src_dir = os.path.join(project_root, "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)


def plugin_file_path(
    plugin_name: str,
    plugins_dir: str = "plugins",
    project_root: str = PROJECT_ROOT,
) -> str:
    """返回插件源码文件路径。"""
    plugins_path = os.path.join(project_root, plugins_dir)
    flat_path = os.path.join(plugins_path, f"{plugin_name}.py")
    if os.path.isfile(flat_path):
        return flat_path
    return os.path.join(plugins_path, plugin_name, "__init__.py")


def discover_plugin_names(
    plugins_dir: str = "plugins",
    project_root: str = PROJECT_ROOT,
) -> list[str]:
    """扫描目录中的插件名。"""
    path = os.path.join(project_root, plugins_dir)
    if not os.path.isdir(path):
        return []

    names: list[str] = []
    for name in sorted(os.listdir(path)):
        if name.startswith("_"):
            continue
        item_path = os.path.join(path, name)
        if name.endswith(".py") and os.path.isfile(item_path):
            names.append(name[:-3])
            continue
        if os.path.isdir(item_path) and os.path.isfile(os.path.join(item_path, "__init__.py")):
            names.append(name)
    return names


def load_plugin_module(
    plugin_name: str,
    plugins_dir: str = "plugins",
    project_root: str = PROJECT_ROOT,
) -> tuple[ModuleType, str]:
    """按插件名加载模块对象。"""
    ensure_src_on_path(project_root)
    filepath = plugin_file_path(plugin_name, plugins_dir, project_root)
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"插件不存在: {plugin_name}")

    module_name = f"plugins.{plugin_name}"
    spec_kwargs: dict[str, Any] = {}
    if os.path.basename(filepath) == "__init__.py":
        spec_kwargs["submodule_search_locations"] = [os.path.dirname(filepath)]
    spec = importlib.util.spec_from_file_location(module_name, filepath, **spec_kwargs)
    if not spec or not spec.loader:
        raise ImportError(f"无法创建模块 spec: {plugin_name}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module, module_name


def find_plugin_class(module: Any) -> type[BotModule] | None:
    """在模块中查找 `BotModule` 子类。"""
    for attr_name in dir(module):
        try:
            obj = getattr(module, attr_name)
            if isinstance(obj, type) and issubclass(obj, BotModule) and obj is not BotModule:
                return obj
        except (TypeError, AttributeError):
            continue
    return None


def build_plugin_instance(
    plugin_name: str,
    plugins_dir: str = "plugins",
    project_root: str = PROJECT_ROOT,
) -> BotModule:
    """按插件名实例化插件对象。"""
    module, _module_name = load_plugin_module(plugin_name, plugins_dir, project_root)
    plugin_class = find_plugin_class(module)
    if not plugin_class:
        raise TypeError(f"插件未定义 BotModule 子类: {plugin_name}")
    return plugin_class()
