from core.constants import Msg
from domain.plugins.plugin_operation import PluginOperationCode, PluginOperationResult

_INVALID_PLUGIN_NAME_MESSAGE = f"{Msg.ERR} 插件名不合法，仅支持字母/数字/下划线"

_CODE_TEMPLATES: dict[PluginOperationCode, tuple[str, str]] = {
    PluginOperationCode.NOT_FOUND: (Msg.ERR, "未找到插件: {plugin_name}"),
    PluginOperationCode.ALREADY_LOADED: (Msg.ERR, "插件已加载: {plugin_name}"),
    PluginOperationCode.INVALID_SPEC: (Msg.ERR, "插件加载描述无效: {plugin_name}"),
    PluginOperationCode.INVALID_MODULE: (Msg.ERR, "插件未定义 BotModule 子类: {plugin_name}"),
    PluginOperationCode.REGISTER_FAILED: (Msg.ERR, "插件注册失败: {plugin_name}"),
    PluginOperationCode.INVALID_CONFIG: (Msg.ERR, "{message}"),
    PluginOperationCode.ON_LOAD_FAILED: (Msg.ERR, "{message}"),
    PluginOperationCode.INSTANTIATION_FAILED: (Msg.ERR, "{message}"),
    PluginOperationCode.BUILTIN_FORBIDDEN: (Msg.ERR, "内置插件不允许动态卸载: {plugin_name}"),
    PluginOperationCode.NOT_LOADED: (Msg.ERR, "插件未加载: {plugin_name}"),
    PluginOperationCode.LOAD_FAILED: (Msg.ERR, "{message}"),
}


def format_invalid_plugin_name_message() -> str:
    """返回统一的非法插件名提示。"""
    return _INVALID_PLUGIN_NAME_MESSAGE


def format_plugin_operation_message(result: PluginOperationResult) -> str:
    """根据结果码生成统一的用户提示。"""
    if result.code == PluginOperationCode.SUCCESS:
        return _format_success_message(result)

    prefix, template = _CODE_TEMPLATES.get(
        result.code,
        (Msg.OK if result.ok else Msg.ERR, "{message}"),
    )
    plugin_name = result.plugin_name or "未知插件"
    body = template.format(plugin_name=plugin_name, message=result.message)
    return f"{prefix} {body}"


def _format_success_message(result: PluginOperationResult) -> str:
    plugin_name = result.plugin_name or "未知插件"
    message = result.message
    if result.plugin_name:
        if message.startswith("已加载"):
            message = f"已加载: {plugin_name}"
        elif message.startswith("已卸载"):
            message = f"已卸载: {plugin_name}"
    return f"{Msg.OK} {message}"
