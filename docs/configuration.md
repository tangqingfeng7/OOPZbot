# 配置说明

## 创建配置文件

```shell
copy config.example.py config.py
copy private_key.example.py private_key.py
```

> 主要登录方式是管理后台账号密码登录；命令行 [凭据获取工具](credential-tool.md) 只作为后台登录不可用时的备用方案。

## config.py 配置项

### Oopz 平台配置 (`OOPZ_CONFIG`)

| 配置项 | 说明 |
|--------|------|
| `login_phone` | Oopz 登录手机号 / 账号。填了以后，启动时会用账号密码刷新凭据 |
| `login_password` | Oopz 登录密码。留空时仍可在管理后台临时输入 |
| `person_uid` | Oopz 用户 UID |
| `device_id` | 设备 ID |
| `jwt_token` | JWT Token |
| `default_area` | 默认区域 ID |
| `default_channel` | 默认频道 ID |
| `base_url` | 网关 API 地址（默认 `https://gateway.oopz.cn`） |
| `use_announcement_style` | Bot 发送消息默认是否使用公告样式（`styleTags: ["IMPORTANT"]`）。可在 admin 后台 → 域配置 → 公告样式 给单个域覆盖 |
| `proxy` | 代理配置：留空走系统代理；`False` / `"direct"` 表示直连 |
| `agora_app_id` | Oopz 语音频道使用的 Agora App ID |
| `agora_init_timeout` | 语音浏览器初始化等待秒数 |

`login_phone`、`login_password` 是主要登录配置。程序启动时如果这两项有值，会先用账号密码直接登录并刷新 `person_uid`、`device_id`、`jwt_token` 和 `private_key.py`。

管理后台的“OOPZ 账号密码登录”使用同一套入口：页面里填了账号密码就用页面输入；页面留空时会改用 `config.py` 里的账号密码。后台会优先调用 Oopz 登录接口，遇到可重试问题时再回退到浏览器登录。

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
| `audio_download_timeout` | 音频下载读超时（秒），弱网可调大（默认 `120`） |
| `audio_download_retries` | 下载失败后重试次数（默认 `2`） |
| `audio_quality` | 音质档位：`"standard"`（体积小/弱网友好）或 `"exhigh"`（音质更好） |

推流播放时会自动预加载队首下一首，减少切歌间隙与卡顿；弱网下可适当调大 `audio_download_timeout`、`audio_download_retries`，或使用 `audio_quality: "standard"`。

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

### LOL 封号查询插件 (`config/plugins/lol_ban/config.json`)

| 配置项 | 说明 |
|--------|------|
| `enabled` | 是否启用（默认 `false`） |
| `api_url` | 查询 API 地址 |
| `token` | API 认证令牌 |
| `proxy` | 代理地址，留空走系统代理 |

### FA8 战绩查询插件 (`config/plugins/lol_fa8/config.json`)

| 配置项 | 说明 |
|--------|------|
| `enabled` | 是否启用（默认 `false`） |
| `username` | FA8 登录账号 |
| `password` | FA8 登录密码 |
| `default_area` | 默认大区 ID（`1`=艾欧尼亚） |

### 三角洲插件 (`config/plugins/delta_force/config.json`)

| 配置项 | 说明 |
|--------|------|
| `enabled` | 是否启用（默认 `true`，代码当前主要依赖是否存在配置与凭据） |
| `api_key` | 三角洲后端 API Key（必填） |
| `client_id` | 三角洲后端 client ID（必填） |
| `api_mode` | API 地址选择模式，默认 `auto` |
| `base_urls` | 后端地址列表，`auto` 模式下按顺序故障切换 |
| `login_timeout_sec` | 二维码登录总超时（秒） |
| `login_poll_interval_sec` | 二维码状态轮询间隔（秒） |
| `login_delivery_mode` | 二维码投递方式：`private_message`（私信）或 `temp_channel`（临时频道） |
| `login_success_notice_delay_sec` | 临时频道模式下，登录结束提示保留多久后自动删频道（秒，默认 `10`） |
| `request_timeout_sec` | 单次 HTTP 请求超时（秒） |
| `request_retries` | 单个后端地址最大重试次数 |
| `daily_keyword_push_check_interval_sec` | 每日密码定时推送检查间隔（秒，默认 `60`，最小 `30`） |
| `daily_keyword_push_time` | 每日密码定时推送时间（`HH:MM`，默认 `08:00`） |
| `place_push_interval_sec` | 特勤处制造完成推送轮询间隔（秒，默认 `60`，最小 `15`） |
| `render_timeout_sec` | 海报渲染超时（秒） |
| `render_width` | 海报截图宽度 |
| `render_scale` | 预留渲染缩放参数 |
| `temp_dir` | 三角洲插件运行时目录（用于二维码和临时渲染文件） |

说明：

- `login_delivery_mode` 默认为 `private_message`。
- 设为 `temp_channel` 时，插件会创建仅登录用户可见的临时文字频道发送二维码；登录成功、超时、过期或其他终止场景提示后会自动删频道。
- 若所选投递方式失败，插件会回退到当前频道发送提示或二维码，避免登录流程直接中断。
- `temp_dir/qrs` 会在插件加载时自动清理过期二维码文件。
- 每日密码定时推送会按 `daily_keyword_push_time` 在所有已订阅频道每日推送一次。
- 特勤处制造完成推送会按 `place_push_interval_sec` 周期轮询当前已订阅频道，并在检测到生产任务完成时推送到原频道。

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

### Web 播放器 (`WEB_PLAYER_CONFIG`)

| 配置项 | 说明 |
|--------|------|
| `host` | 监听地址（默认 `0.0.0.0`） |
| `port` | 监听端口（默认 `8080`）；使用 Nginx 反代时此端口仅内部访问 |
| `url` | 对外访问地址，留空则自动检测公网 IP；使用 Nginx 反代时填写 `https://你的域名` |
| `token_ttl_seconds` | Web 随机访问令牌有效期（秒），`0` 表示不过期（不建议） |
| `cookie_max_age_seconds` | 浏览器 cookie 有效期（秒）；留空时默认跟 `token_ttl_seconds` 一致 |
| `cookie_secure` | 是否仅在 HTTPS 下发送 cookie（使用 Nginx + SSL 时设为 `True`） |
| `link_idle_release_seconds` | 播放列表空闲超时后释放随机链接（秒，`0` 表示不释放） |

> **注意**：长期配置只认 `config.py`。管理后台 `/admin` -> 配置页面保存时会写回 `config.py`，并同步更新当前进程，不需要重启。

### OneBot v11 旁路服务 (`ONEBOT_V11_CONFIG`)

OneBot v11 旁路服务默认关闭。启用后，当前 Oopz Bot 会继续照常运行，同时额外提供 OneBot v11 HTTP / WebSocket 接口，适合接 NoneBot、AstrBot、Hoshino 等外部程序。

| 配置项 | 说明 |
|--------|------|
| `enabled` | 是否启用（默认 `False`） |
| `host` | 监听地址（默认 `127.0.0.1`） |
| `port` | 监听端口（默认 `6700`） |
| `access_token` | OneBot 鉴权 token，留空则不鉴权 |
| `secret` | HTTP POST 上报签名密钥 |
| `db_path` | OneBot 数字 ID 与消息映射库，默认 `data/onebot_v11.sqlite3` |
| `enable_http` | 是否启用 HTTP action 接口 |
| `enable_ws` | 是否启用正向 WebSocket |
| `enable_http_post` | 是否启用 HTTP POST 事件上报 |
| `http_post_urls` | HTTP POST 上报地址列表 |
| `http_post_timeout` | HTTP POST 上报超时秒数，`0` 表示使用默认超时 |
| `enable_ws_reverse` | 是否启用反向 WebSocket |
| `ws_reverse_url` | 反向 WebSocket Universal 地址 |
| `ws_reverse_api_url` | 反向 WebSocket API 地址 |
| `ws_reverse_event_url` | 反向 WebSocket Event 地址 |
| `ws_reverse_reconnect_interval` | 反向 WebSocket 断线重连间隔秒数 |
| `send_connect_event` | WebSocket 连接建立后是否发送生命周期事件 |
| `enable_area_scoped_group_ban` | 是否启用 `set_group_ban`，会映射为 Oopz 域成员禁言 |
| `enable_set_group_kick_as_area_kick` | 是否启用 `set_group_kick`，会映射为移出域或封禁 |
| `enable_set_group_leave_as_area_leave` | 是否启用 `set_group_leave`，会映射为离开对应 Oopz 域 |

详细使用方式见 [OneBot v11 旁路适配](onebot-v11.md)。

### 自动撤回 (`AUTO_RECALL_CONFIG`)

| 配置项 | 说明 |
|--------|------|
| `enabled` | 是否启用自动撤回（默认 `False`） |
| `delay` | 自动撤回延迟秒数（默认 `30`） |
| `exclude_commands` | 不自动撤回的命令类型列表，如 `ai_chat`、`ai_image` |

### 域成员加入/退出通知 (`AREA_JOIN_NOTIFY`)

用户加入或退出当前域时，Bot 在公屏发送欢迎/再见消息。**退出**依赖 WebSocket 推送（event 11 等）；**加入**因服务端不推送，改为轮询域成员 API 检测新成员。

| 配置项 | 说明 |
|--------|------|
| `enabled` | 是否启用（默认 `False`） |
| `message_template` | 加入时消息模板，占位符：`{name}`、`{uid}`（默认 `"欢迎 {name} 加入域～"`） |
| `message_template_leave` | 退出时消息模板，占位符：`{name}`、`{uid}`（默认 `"{name} 已退出域"`） |
| `poll_interval_seconds` | 轮询间隔（秒），最小 2；默认 2。若成员接口返回 429，程序会自动退避并临时放慢轮询 |

需配置 `default_area`、`default_channel`（或由 Bot 自动取第一个已加入域及第一个文字频道）。通知消息与 Bot 其他消息一致，默认使用**公告样式**。

### 权限控制 (`ADMIN_UIDS`)

管理员 UID 列表。列表为空时不限制权限，所有用户均可执行管理命令。

### 名称映射 (`NAME_MAP`)

手动配置 ID → 显示名称的映射，包含 `users`、`channels`、`areas` 三个子字典。Bot 运行时会自动发现新 ID 并记录到 `data/names.json`。

### Agora 语音频道 (`agora_app_id`)

`OOPZ_CONFIG["agora_app_id"]` 为 Oopz 平台使用的 Agora App ID，用于语音频道推流。通过无头浏览器运行 Agora Web SDK。

#### 浏览器后端

| 后端 | 说明 |
|------|------|
| **Playwright**（优先） | 安装：`pip install playwright` 后执行 `playwright install chromium`。Linux/macOS 推荐。 |
| **Selenium**（回退） | 当 Playwright 不可用时自动启用（如 Windows 上 greenlet DLL 错误）。需已安装 `selenium`、`webdriver-manager`（见 `requirements.txt`），以及本机 **Chrome** 或 **Edge** 浏览器。 |

程序会依次尝试：Playwright → Selenium（Chrome，含 webdriver-manager / Selenium Manager）→ Selenium（Edge，仅 Windows）。启动成功时日志会显示 `后端=playwright` 或 `后端=selenium`。

#### 故障排除

- **“DLL load failed while importing _greenlet”**  
  Windows 上 Playwright 依赖的 greenlet 可能缺 VC++ 运行库。无需处理，程序会自动改用 Selenium；确保已安装 Chrome 或 Edge 并执行 `pip install -r requirements.txt`。

- **“Unable to obtain driver for chrome”**  
  Selenium 无法找到或下载 ChromeDriver。可尝试：  
  1. 确认本机已安装 [Chrome](https://www.google.com/chrome/) 或 Edge，并重新运行 `python main.py`（会尝试多种方式及 Edge）。  
  2. 手动下载与 Chrome 版本匹配的 [ChromeDriver](https://googlechromelabs.github.io/chrome-for-testing/)（如 chromedriver-win64.zip），解压后将 `chromedriver.exe` 所在目录加入系统 **PATH**。  
  3. Edge 驱动可从此处下载并加入 PATH：[Edge WebDriver](https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/)。

- **不启用语音推流**  
  在 `OOPZ_CONFIG` 中不填 `agora_app_id`（或留空），则不会初始化浏览器，音乐点歌仍可用，仅无法在语音频道内播放。

## 环境变量覆盖

Docker 环境下可通过环境变量覆盖部分配置，无需修改 `config.py`：

| 环境变量 | 对应配置 |
|----------|----------|
| `BOT_REDIS_HOST` / `BOT_REDIS_PORT` / `BOT_REDIS_PASSWORD` / `BOT_REDIS_DB` | Redis 连接 |
| `BOT_NETEASE_BASE_URL` | 网易云 API 地址 |
| `BOT_WEB_HOST` / `BOT_WEB_PORT` | Web 播放器监听 |
| `BOT_OOPZ_PROXY` | Oopz 代理地址 |
| `BOT_DISABLE_VOICE` | 禁用语音推流 |
| `BOT_DISABLE_AUTO_START_NETEASE` | 禁用自动启动网易云 API |

## 插件配置

插件配置位于 `config/plugins/` 目录。现在每个插件一个子目录，例如 `config/plugins/delta_force/config.json`、`config/plugins/delta_force/example.json`、`config/plugins/delta_force/schema.json`。复制 `example.json` 为 `config.json` 后修改即可。

## private_key.py

RSA 私钥（PEM 格式）用于 Oopz API 请求签名。推荐通过管理后台登录自动写入；手动填写时支持 PKCS#1（`-----BEGIN RSA PRIVATE KEY-----`）和 PKCS#8（`-----BEGIN PRIVATE KEY-----`）两种格式。
