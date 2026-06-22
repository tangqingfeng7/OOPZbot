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
                ┌───────────────┐
                │command_handler│  指令路由 · 权限校验 · 脏话检测
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
│   │   └── command_handler.py   # 命令路由（@bot 指令 + / 命令 + 权限校验 + 脏话自动禁言）
│   ├── core/                    # 基础设施
│   │   ├── database.py          # SQLite 数据层
│   │   ├── logger_config.py     # 日志配置
│   │   ├── queue_manager.py     # Redis 播放队列管理
│   │   ├── redis_keys.py        # Redis 键名与域隔离键（单一来源）
│   │   ├── constants.py         # 跨模块常量（消息前缀 Msg、@提及、UA）
│   │   ├── json_utils.py        # 紧凑 JSON 序列化（保证签名字节一致）
│   │   ├── paths.py             # 项目路径唯一来源
│   │   ├── proxy_utils.py       # 代理配置
│   │   └── area_config.py       # 域配置
│   ├── music/                   # 音乐与语音播放
│   │   ├── music.py             # 音乐核心调度
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
│   │   └── name_resolver.py     # ID → 名称解析
│   ├── onebot_v11/              # OneBot v11 旁路适配（adapter / server / store / message / config）
│   ├── services/                # 独立服务
│   │   ├── chat.py              # AI 聊天 + 图片生成
│   │   ├── area_join_notifier.py # 域成员加入/退出通知
│   │   ├── scheduler_service.py # 定时任务服务
│   │   └── conversation_memory.py # AI 上下文记忆
│   ├── web/                     # Web 播放器与 Admin 后台
│   │   ├── web_player.py        # FastAPI 主应用
│   │   ├── web_player_admin.py  # Admin 后台路由
│   │   ├── web_player_config.py # Web / Admin 配置管理
│   │   ├── web_link_token.py    # Web 播放器访问令牌
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
│   ├── convert_subscription.py  # 代理订阅转 Clash/Mihomo 配置
│   └── audio_service.py         # 音频播放服务（ffplay + FastAPI）
│
├── nginx/                       # Nginx 反向代理配置
│   ├── nginx.conf               # 站点配置（HTTP + HTTPS，支持裸机 / Docker 两种 upstream）
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
| `src/web/web_player_admin.py` | Admin 后台所有路由（`APIRouter`），包括登录、概览、统计、配置、队列管理等 |
| `src/web/web_player_config.py` | 配置常量（`WEB_PLAYER_CONFIG` 引用）、分组定义、基线值、config.py 写回与热更新 |

`src/web/web_player.py` 通过 `app.include_router(admin_router)` 挂载 Admin 路由。

## 数据库表结构

| 表名 | 用途 | 关键字段 |
|------|------|----------|
| `image_cache` | 封面图片缓存 | source_id, oopz_url, use_count |
| `song_cache` | 歌曲信息缓存 | song_id, song_name, artist, play_count |
| `play_history` | 播放历史记录 | song_cache_id, channel_id, user_id, played_at |
| `statistics` | 每日统计汇总 | date, total_plays, unique_songs, cache_hits |

## Web 播放器

### 架构总览

Web 播放器通过 FastAPI 提供 HTTP API，前端 `player.html` 通过轮询获取状态、歌词、队列，通过 POST 请求发送控制命令。Admin 后台路由由 `src/web/web_player_admin.py` 通过 `APIRouter` 提供，配置管理由 `src/web/web_player_config.py` 集中处理。

```
浏览器 (player.html / admin 页面)
  │
  ▼
Nginx / OpenResty (:80 HTTP, :443 HTTPS)
  │  / → bot:8080,  /netease-api/ → netease-api:3000
  ▼
src/web/web_player.py ──► src/web/web_player_admin.py (APIRouter)
(FastAPI :8080)        └── src/web/web_player_config.py (配置管理)
  │  读取 Redis: music:current, music:queue, music:play_state, music:volume
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

| 键 | 类型 | 说明 |
|----|------|------|
| `music:current` | String (JSON) | 当前播放歌曲信息（song_id, name, artist, cover, duration_ms 等） |
| `music:queue` | List (JSON[]) | 播放队列，每个元素为歌曲 JSON |
| `music:play_state` | String (JSON) | 播放状态（start_time, duration, paused, pause_elapsed） |
| `music:volume` | String | 当前音量 0-100 |
| `music:web_commands` | List | Web 控制命令队列，由 BLPOP 实时消费 |

### Web 控制命令

命令通过 `RPUSH` 写入 `music:web_commands`，`music.py` 的独立监听线程通过 `BLPOP` 实时取出执行（延迟 < 100ms）。

| 命令 | 说明 |
|------|------|
| `next` | 切下一首 |
| `stop` | 停止播放并清空队列 |
| `pause` | 暂停 |
| `resume` | 恢复播放 |
| `seek:<秒数>` | 跳转到指定位置 |
| `volume:<0-100>` | 设置音量 |
| `notify:<json>` | Web 点歌后在频道发送通知消息 |

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
