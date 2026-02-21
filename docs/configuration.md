# 配置说明

## 创建配置文件

```shell
copy config.example.py config.py
copy private_key.example.py private_key.py
```

> 也可以通过 [凭据获取工具](credential-tool.md) 自动生成 `config.py` 和 `private_key.py`。

## config.py 配置项

### Oopz 平台配置 (`OOPZ_CONFIG`)

| 配置项 | 说明 |
|--------|------|
| `person_uid` | Oopz 用户 UID |
| `device_id` | 设备 ID |
| `jwt_token` | JWT Token |
| `default_area` | 默认区域 ID |
| `default_channel` | 默认频道 ID |
| `base_url` | 网关 API 地址（默认 `https://gateway.oopz.cn`） |
| `api_url` | 公共 API 地址（默认 `https://api.oopz.cn`） |

### Redis 配置 (`REDIS_CONFIG`)

| 配置项 | 说明 |
|--------|------|
| `host` | Redis 地址（默认 `127.0.0.1`） |
| `port` | Redis 端口（默认 `6379`） |
| `password` | Redis 密码（默认为空） |
| `db` | 数据库编号（默认 `0`） |

### 网易云音乐 (`NETEASE_CLOUD`)

| 配置项 | 说明 |
|--------|------|
| `base_url` | 网易云 API 服务地址（默认 `http://localhost:3000`） |
| `cookie` | 登录后的 MUSIC_U Cookie（可选） |
| `auto_start_path` | 相对于项目根目录的 API 目录名（如 `"NeteaseCloudMusicApi"`），留空则不自动启动 |

### 豆包 AI 聊天 (`DOUBAO_CONFIG`)

| 配置项 | 说明 |
|--------|------|
| `enabled` | 是否启用（默认 `False`） |
| `base_url` | 火山方舟 API 地址 |
| `api_key` | 火山方舟 API Key |
| `model` | 模型名称 |
| `system_prompt` | 系统提示词 |
| `max_tokens` | 最大生成 token 数 |
| `temperature` | 生成温度 |

### 豆包图片生成 (`DOUBAO_IMAGE_CONFIG`)

| 配置项 | 说明 |
|--------|------|
| `enabled` | 是否启用（默认 `False`） |
| `api_key` | 火山方舟 API Key |
| `model` | Seedream 模型名称 |
| `size` | 图片尺寸（默认 `1920x1920`） |

### LOL 封号查询 (`LOL_BAN_CONFIG`)

| 配置项 | 说明 |
|--------|------|
| `enabled` | 是否启用（默认 `False`） |
| `api_url` | 查询 API 地址 |
| `token` | API 认证令牌 |

### FA8 战绩查询 (`FA8_CONFIG`)

| 配置项 | 说明 |
|--------|------|
| `enabled` | 是否启用（默认 `False`） |
| `username` | FA8 登录账号 |
| `password` | FA8 登录密码 |
| `default_area` | 默认大区 ID（`1`=艾欧尼亚） |

### 脏话自动禁言 (`PROFANITY_CONFIG`)

| 配置项 | 说明 |
|--------|------|
| `enabled` | 是否启用（默认 `True`） |
| `mute_duration` | 禁言时长（分钟），仅支持 `1`/`5`/`60`/`1440`/`4320`/`10080` |
| `recall_message` | 是否自动撤回违规消息（默认 `True`） |
| `skip_admins` | 管理员是否免检（默认 `True`） |
| `warn_before_mute` | 是否先警告再禁言（默认 `False`，即直接禁言） |
| `context_detection` | 上下文拆字检测（默认 `True`） |
| `context_window` | 上下文时间窗口，秒（默认 `30`） |
| `context_max_messages` | 上下文最多回溯消息条数（默认 `10`） |
| `ai_detection` | AI 辅助检测，需启用豆包 AI（默认 `True`） |
| `ai_min_length` | 触发 AI 检测的最短消息长度（默认 `2`） |
| `keywords` | 敏感词列表，支持自定义扩展 |

### 聊天自动回复 (`CHAT_CONFIG`)

| 配置项 | 说明 |
|--------|------|
| `enabled` | 是否启用（默认 `True`） |
| `keyword_replies` | 关键词 → 回复内容的映射字典 |

### 权限控制 (`ADMIN_UIDS`)

管理员 UID 列表。列表为空时不限制权限，所有用户均可执行管理命令。

### 名称映射 (`NAME_MAP`)

手动配置 ID → 显示名称的映射，包含 `users`、`channels`、`areas` 三个子字典。Bot 运行时会自动发现新 ID 并记录到 `data/names.json`。

### Agora 语音频道 (`agora_app_id`)

`OOPZ_CONFIG["agora_app_id"]` 为 Oopz 平台使用的 Agora App ID，用于语音频道推流。通过 Playwright 控制无头 Chromium 运行 Agora Web SDK，全平台兼容。

```bash
pip install playwright
playwright install chromium
```

## private_key.py

粘贴 RSA 私钥（PEM 格式），用于 Oopz API 请求签名。支持 PKCS#1（`-----BEGIN RSA PRIVATE KEY-----`）和 PKCS#8（`-----BEGIN PRIVATE KEY-----`）两种格式。
