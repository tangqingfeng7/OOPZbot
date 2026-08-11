# src 目录说明

`src` 里优先按职责放文件：

- `app/`：应用启动、生命周期、运行时服务和插件加载。
- `bot/`：Bot 消息入口和命令处理入口。
- `core/`：日志、数据库、队列、代理和域配置等基础设施。
- `domain/`：纯业务规则、插件契约和数据结构。
- `music/`：音乐搜索、播放控制、平台适配和语音推流。
- `oopz/`：OOPZ WebSocket、HTTP API、消息发送、上传、登录和名称解析。
- `onebot_v11/`：OneBot v11 的**配置转换、能力补丁与数据迁移**。协议本体（adapter / server /
  store / message）已由内置 SDK 接管，这里只保留 SDK 未覆盖的部分：把项目配置翻译成
  `OneBotV11ServerConfig`（`config.py`）、补齐 SDK v0.15.0 缺失的成员进出通知、
  `meta_event` 心跳、`set_group_admin` 与 `get_group_msg_history` 动作（`sdk_integration.py`）、
  以及旧库结构到 SDK 结构的一次性迁移（`sdk_migration.py`）。
- `services/`：聊天、定时任务、入退通知等独立服务。
- `web/`：Web 播放器、Admin 后台和前端资源。

新增插件源码放在仓库根目录的 `plugins/`，插件配置放在 `config/plugins/`。
