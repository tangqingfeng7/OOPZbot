# 系统架构

## 架构图

```
                    Oopz 平台
                       │
                  WebSocket 连接
                       │
                       ▼
                 ┌─────────────┐
                 │ oopz_client │  心跳保活 · 自动重连 · 事件分发
                 └──────┬──────┘
                        │
                        ▼
               ┌──────────────────┐
               │message_dispatcher│  有界分片 · 同频道保序 · 背压
               └────────┬─────────┘
                        ▼
                ┌───────────────┐
                │command_handler│  运行时组装 · 统计/安全预检 · 委托路由
                └─┬──┬──┬──┬───┘
                  │  │  │  │
        ┌─────────┘  │  │  └──────────┐
        ▼            ▼  ▼             ▼
   ┌─────────┐  ┌──────────┐  ┌────────────┐
   │ music   │  │  chat    │  │  plugins   │
   │         │  │          │  │            │
   │ 搜索/队列│  │ AI聊天   │  │ 扩展命令   │
   │ 播放/缓存│  │ AI画图   │  └────────────┘
   └────┬────┘  │ AI审核   │
        │       └────┬─────┘
  ┌─────┴─────┐      │
  ▼           ▼      └──► 豆包 AI API
netease    queue_manager
(API)       (Redis)
  │
  ▼
NeteaseCloud API (:3000)

         ┌─────────────────────────────────┐
         │          oopz_sender            │
         │  OopzSender(UploadMixin,        │
         │             OopzApiMixin)       │
         │  RSA 签名 · 消息发送 · 上传 · API │
         └──────────────┬──────────────────┘
                        │
                   ┌────┴────┐
                   ▼         ▼
              Oopz API   Oopz CDN
                             │
                        database (SQLite)

  ┌──────────────────────┐   Redis    ┌──────────────────────┐
  │    web_player        │◄─────────►│  music               │
  │  ├ web_player_admin  │ web_cmd   │  └ music_playback    │
  │  └ web_player_config │ play_st   │                      │
  │    (FastAPI :8080)   │ volume    │  voice_client        │
  └──────────┬───────────┘           └──────────┬───────────┘
             │                                  │
   ┌─────────┴──────────┐                Agora RTC (语音频道)
   │  Nginx / OpenResty │                       │
   │  :80 → 301 HTTPS   │              agora_player.html
   │  :443 (HTTPS+SSL)  │           浏览器自动化（Playwright/Selenium）
   └─────────┬──────────┘           音频推流/暂停/跳转/音量
             │
        浏览器 (Web UI)
             │
        player.html
        搜索/点歌/控制
        暂停/进度/音量
```

## 技术栈

| 类别 | 技术 |
|------|------|
| 运行时 | Python 3.10+ |
| WebSocket | websocket-client |
| Web 服务 | FastAPI + Uvicorn（Web 播放器 :8080）+ Nginx / OpenResty（反向代理 :80/:443） |
| 队列 | Redis（播放队列 + 播放状态 + Web 命令通道） |
| 数据库 | SQLite（缓存、统计） |
| 加密签名 | cryptography（RSA PKCS1v15 + SHA256） |
| AI 接口 | 豆包（火山方舟，OpenAI 兼容） |
| 音乐 API | NeteaseCloudMusicApi（Node.js） |
| 语音推流 | Agora Web SDK（Playwright 优先，Selenium 回退） |

## 项目结构

```
├── main.py                      # 入口：初始化数据库、启动 Bot
├── config.py                    # 集中配置（平台、Redis、AI、音乐等）
├── config.example.py            # 配置示例
├── private_key.py               # RSA 私钥（PEM 格式）
├── private_key.example.py       # 私钥示例
├── requirements.txt             # Python 依赖
│
├── src/                         # 核心源码模块
│   ├── bot/                     # Bot 消息入口
│   │   └── command_handler.py   # 运行时协调入口：组装服务/插件，统计与安全预检后委托路由服务
│   ├── core/                    # 基础设施
│   │   ├── database.py          # SQLite 数据层
│   │   ├── logger_config.py     # 日志配置
│   │   ├── queue_manager.py     # Redis 播放队列管理
│   │   ├── message_dispatcher.py # 有界分片队列（同频道保序、跨频道并行、背压）
│   │   ├── redis_keys.py        # Redis 键名与域隔离键（单一来源）
│   │   ├── browser_launch.py    # 语音推流与 OOPZ 登录共用的 Chromium 启动参数
│   │   ├── constants.py         # 跨模块常量（消息前缀 Msg、@提及、UA）
│   │   ├── json_utils.py        # 紧凑 JSON 序列化（保证签名字节一致）
│   │   ├── paths.py             # 项目路径唯一来源
│   │   ├── http_constants.py    # HTTP 客户端默认超时分档（单一来源）
│   │   ├── proxy_utils.py       # 代理配置
│   │   └── area_config.py       # 域配置
│   ├── music/                   # 音乐与语音播放
│   │   ├── music.py             # 音乐核心调度
│   │   ├── music_platform.py    # 音乐平台统一协议与注册表
│   │   ├── music_playback.py    # 播放执行、推流、链接生成
│   │   ├── music_web_control.py # Web 控制命令消费与分发
│   │   ├── netease.py           # 网易云音乐 API 封装
│   │   ├── bilibili_music.py    # B 站音频搜索
│   │   ├── qq_music.py          # QQ 音乐适配
│   │   └── voice_client.py      # Agora 语音客户端
│   ├── oopz/                    # OOPZ 通信
│   │   ├── oopz_client.py       # WebSocket 客户端
│   │   ├── oopz_sender.py       # 消息发送核心
│   │   ├── oopz_upload.py       # 文件/图片/音频上传
│   │   ├── oopz_api.py          # OOPZ 平台 API 交互
│   │   ├── responses.py         # API 响应归一化（ApiResult / parse_*）
│   │   ├── signing.py           # 请求签名唯一来源（RSA + Oopz-* 头）
│   │   ├── oopz_password_login.py # OOPZ 账号密码登录
│   │   ├── area_events.py       # 域成员进出 WebSocket 事件的共享解析
│   │   └── name_resolver.py     # ID → 名称解析
│   ├── onebot_v11/              # OneBot v11 旁路适配（adapter / server / store / message / config）
│   ├── services/                # 独立服务
│   │   ├── chat.py              # AI 聊天 + 图片生成
│   │   ├── area_join_notifier.py # 域成员加入/退出通知
│   │   ├── scheduler_service.py # 定时任务服务
│   │   ├── scheduler_templates.py # 定时消息模板预设
│   │   └── conversation_memory.py # AI 上下文记忆
│   ├── web/                     # Web 播放器与 Admin 后台
│   │   ├── web_player.py        # FastAPI 主应用
│   │   ├── web_player_admin.py  # Admin 路由入口（聚合 web.admin 包，对外稳定 facade）
│   │   ├── web_player_config.py # Web / Admin 配置管理
│   │   ├── web_link_token.py    # Web 播放器访问令牌
│   │   ├── admin/               # Admin 后台路由包（pages / auth / config / music / scheduler / plugins / members / shared）
│   │   └── assets/              # Web 前端资源
│   │       ├── player.html      # Web 播放器前端
│   │       ├── agora_player.html # Agora RTC 浏览器端
│   │       ├── agora_sdk.js     # Agora Web SDK 本地缓存
│   │       └── admin/           # Admin 后台前端资源
│   ├── app/                     # 应用启动、运行时和服务编排
│   └── domain/                  # 业务规则、插件契约和数据结构
│
├── plugins/                     # 插件目录
│   ├── delta_force/             # 三角洲插件
│   │   ├── __init__.py          # 插件入口
│   │   ├── api.py               # API 封装
│   │   ├── assets.py            # 静态资源辅助
│   │   ├── login.py             # 登录流程
│   │   ├── store.py             # 本地状态存储
│   │   ├── render.py            # 海报渲染
│   │   ├── formatters.py        # 文案格式化
│   │   ├── daily_push.py        # 每日密码推送
│   │   ├── place_push.py        # 特勤处推送
│   │   └── assets/              # 静态资源
│   ├── lol_ban/                 # LOL 封号查询插件
│   ├── lol_fa8/                 # LOL 战绩查询插件
│   ├── apex/                    # Apex Legends 战绩与游戏信息查询插件
│   ├── arc_raiders/             # ARC Raiders 物品与掉率查询插件
│   ├── steam_price/             # Steam 游戏价格查询与降价提醒插件
│   ├── _shared/                 # 插件共享基类与小工具（IntervalWorker 后台线程 / JsonHttpClient）
│   └── README.md                # 插件说明
│
├── tools/                       # 独立工具
│   ├── credential_tool.py       # 凭据获取工具（RSA 私钥、UID、设备 ID、JWT Token）
│   ├── create_plugin_scaffold.py # 插件脚手架生成
│   ├── export_plugin_config_assets.py # 导出插件配置示例与 schema
│   ├── prepare_clash_config.py  # 规整 Clash/Mihomo 本地启动配置
│   └── convert_subscription.py  # 代理订阅转 Clash/Mihomo 配置
│
├── nginx/                       # Nginx 反向代理配置
│   ├── nginx.conf               # 裸机站点配置（回环 upstream）
│   ├── nginx.docker.conf        # Docker Compose 站点配置（服务名 upstream）
│   └── ssl/                     # SSL 证书目录（.gitignore 忽略证书文件）
│       ├── cert.pem             # 证书（含完整链）
│       └── key.pem              # 私钥
│
├── data/                        # 运行时数据（自动生成）
│   ├── names.json               # ID → 名称缓存
│   └── oopz_cache.db            # SQLite 数据库文件
│
├── docs/                        # 文档目录
└── logs/                        # 日志文件
```

## 模块拆分设计

### OopzSender 模块拆分

`oopz_sender.py` 通过 Mixin 模式拆分为三个模块：

| 模块 | 职责 |
|------|------|
| `oopz_sender.py` | 核心发送器，HTTP 请求基础设施（`_request` 统一方法）、消息发送 |
| `oopz_upload.py` | `UploadMixin`：文件上传、图片上传、音频上传、图片信息获取 |
| `oopz_api.py` | `OopzApiMixin`：所有 Oopz 平台 API 交互（成员管理、频道操作、角色分配等） |

`OopzSender` 继承 `UploadMixin` 和 `OopzApiMixin`，外部调用方式不变。

签名与响应解析已各自收口到单一来源：`signing.py` 提供 `oopz_auth_headers` / `build_oopz_sign`（`oopz_sender`、`name_resolver`、`oopz_password_login` 共用）；`responses.py` 提供 `ApiResult` / `parse_api_response` / `parse_mutation_response`，`oopz_api` 与 `oopz_sender` 统一经其归一化状态码、JSON 与业务 `status` 字段。

### Music 模块拆分

| 模块 | 职责 |
|------|------|
| `music.py` | 音乐核心调度：搜索、队列管理、封面缓存、Web 控制消费 |
| `music_playback.py` | `PlaybackMixin`：播放执行（推流/自动切歌/预加载）、IP 检测、Web 播放器链接生成 |

`MusicHandler` 继承 `PlaybackMixin`。

### Web Player 模块拆分

| 模块 | 职责 |
|------|------|
| `src/web/web_player.py` | FastAPI 主应用实例、播放器 API 路由、共享状态（Redis/Netease 客户端） |
| `src/web/web_player_admin.py` | 9 行稳定 facade（外观入口）：调用 `create_admin_router()` 并对外导出 `admin_router` |
| `src/web/admin/` | Admin 后台的实际路由包；按登录、页面、配置、音乐、定时任务、插件、成员与共享辅助拆分 |
| `src/web/web_player_config.py` | 配置常量（`WEB_PLAYER_CONFIG` 引用）、分组定义、基线值、config.py 写回与热更新 |

`src/web/web_player.py` 仍从 facade 导入 `admin_router`，再通过
`app.include_router(admin_router)` 挂载 `src/web/admin/` 组装的 Admin 路由。

## 数据库表结构

下表是 `src/core/database.py::init_database()` 在应用启动时创建的 10 张核心表。
Steam 插件和 OneBot store 还会按需创建各自的表，不计入这 10 张。

| 表名 | 用途 | 关键字段 |
|------|------|----------|
| `image_cache` | 封面图片缓存 | source_id, oopz_url, use_count |
| `song_cache` | 跨平台歌曲信息与播放次数缓存 | song_id, platform, song_name, play_count |
| `play_history` | 逐次播放历史 | song_cache_id, platform, channel_id, user_id, played_at |
| `statistics` | 每日播放、缓存命中与平台分布汇总 | date, total_plays, cache_hits, cache_misses, platform_breakdown |
| `delta_force_active_token` | 三角洲插件的用户活跃账号组与 framework token | user_id, account_group, framework_token, updated_at |
| `delta_force_place_push` | 三角洲特勤处推送订阅及上次快照 | user_id, channel_id, area_id, last_snapshot, updated_at |
| `delta_force_daily_keyword_push` | 三角洲每日密码推送订阅及去重日期 | channel_id, area_id, last_push_date, updated_at |
| `scheduled_messages` | Admin 配置的定时频道消息 | name, cron_hour, cron_minute, weekdays, channel_id, area_id, enabled |
| `reminders` | 用户定时提醒及执行状态 | user_id, channel_id, area_id, message_text, fire_at, fired |
| `message_stats` | 按日期、频道、域和用户聚合的消息计数 | date, channel_id, area_id, user_id, message_count |

## Web 播放器

### 架构总览

Web 播放器通过 FastAPI 提供 HTTP API，前端 `player.html` 通过轮询获取状态、歌词、队列，通过 POST 请求发送控制命令。Admin 后台路由由 `src/web/admin/` 包组装，`src/web/web_player_admin.py` 只作为稳定 facade，配置管理由 `src/web/web_player_config.py` 集中处理。

```
浏览器 (player.html / admin 页面)
  │
  ▼
Nginx / OpenResty (:80 HTTP, :443 HTTPS)
  │  / → bot:8080,  /netease-api/ → netease-api:3000
  ▼
src/web/web_player.py ──► src/web/web_player_admin.py (facade)
(FastAPI :8080)        │     └── src/web/admin/ (实际 APIRouter)
                        └── src/web/web_player_config.py (配置管理)
  │  读取 Redis: music:<area>:current / queue / play_state, music:volume
  │  写入 Redis: music:web_commands (RPUSH)
  ▼
music.py + music_playback.py (BLPOP 独立线程，实时消费命令)
  │  调用 voice_client 方法
  ▼
voice_client.py → agora_player.html (Playwright 无头浏览器)
  │  Agora Web SDK: 推流/暂停/跳转/音量
  ▼
Agora RTC (语音频道)
```

### Redis 键约定

`QueueManager(area)` 会把播放状态统一写入
`music:<area>:<suffix>`；因此不同域的队列、当前曲目、默认频道、
播放状态和播放模式互不影响。音量、Web 命令通道及 Web/Admin 会话状态为全局键。

| 键或键族 | 类型 | 作用域 | 说明 |
|------------|------|--------|------|
| `music:<area>:queue` | List (JSON[]) | 域隔离 | 指定域的播放队列 |
| `music:<area>:current` | String (JSON) | 域隔离 | 指定域当前播放歌曲 |
| `music:<area>:default_channel` | String | 域隔离 | 指定域的默认播放频道 |
| `music:<area>:play_state` | String (JSON) | 域隔离 | 指定域的进度、时长与暂停状态 |
| `music:<area>:play_mode` | String | 域隔离 | 指定域的 `list` / `single` / `shuffle` 播放模式 |
| `music:volume` | String | 全局 | 当前音量（0–100） |
| `music:web_commands` | List (JSON) | 全局 | 严格 JSON v1 Web 控制命令队列，由 `BLPOP` 实时消费；域命令在载荷中携带 `area` |
| `music:web_access_token` | String | 全局 | `/w/{token}` 播放器访问令牌，可配置 TTL |
| `music:web_active_area` | String | 全局 | 当前 Web 播放器关联的活跃域 |
| `music:web_last_access` | String (timestamp) | 全局 | 播放器最近访问时间，用于空闲释放 |
| `music:admin_session:<token>` | String | 全局键族 | Admin 登录会话存活标记，可配置 TTL |

历史全局播放键会保留在 Redis 中，但运行时不再读取、写入、迁移或删除它们。

### Web 控制命令

命令通过 `RPUSH` 写入 `music:web_commands`，`music.py` 的独立监听线程通过
`BLPOP` 实时取出执行。消费者只接受字段精确匹配的 JSON v1；旧分隔符载荷、
未知版本、未知字段和无域播放命令会被丢弃。

| 作用域 | 必需字段 | 允许操作 |
|--------|----------|----------|
| `area` | `version=1`, `scope=area`, 非空 `area`, `action`, `payload` | next / stop / pause / resume / seek / notify |
| `global` | `version=1`, `scope=global`, `action`, `payload` | 仅 volume（整数 0..100） |

### Web API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/status` | 当前播放状态（歌曲信息、进度、暂停、音量） |
| GET | `/api/queue` | 播放队列 |
| GET | `/api/lyric?id=<song_id>` | 歌词 + 翻译歌词 |
| GET | `/api/search?keyword=<kw>&limit=<n>` | 搜索歌曲 |
| GET | `/api/liked?page=<n>&limit=<n>[&keyword=<kw>]` | 喜欢的音乐（分页）；带 `keyword` 时在全部喜欢中搜索后分页 |
| POST | `/api/add` | 添加歌曲到队列（同时发送频道通知） |
| POST | `/api/control` | 播放控制（action: next/stop/pause/resume/seek/volume） |
| POST | `/api/queue/action` | 队列操作（action: remove/top, index） |
| POST | `/api/liked/refresh` | 刷新喜欢列表缓存 |
