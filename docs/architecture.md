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
   │ music   │  │  chat    │  │ lol_query  │
   │         │  │          │  ├────────────┤
   │ 搜索/队列│  │ AI聊天   │  │ lol_fa8   │
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

                ┌──────────────┐
                │  oopz_sender │  RSA 签名 · 消息发送 · 文件上传
                └──────┬───────┘
                       │
                  ┌────┴────┐
                  ▼         ▼
             Oopz API   Oopz CDN
                            │
                       database (SQLite)
```

## 技术栈

| 类别 | 技术 |
|------|------|
| 运行时 | Python 3.10+ |
| WebSocket | websocket-client |
| 队列 | Redis |
| 数据库 | SQLite（缓存、统计） |
| 加密签名 | cryptography（RSA PKCS1v15 + SHA256） |
| AI 接口 | 豆包（火山方舟，OpenAI 兼容） |
| 音乐 API | NeteaseCloudMusicApi（Node.js） |

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
│   ├── oopz_client.py           # WebSocket 客户端（心跳、重连、事件分发）
│   ├── oopz_sender.py           # 消息发送（RSA 签名、文件上传、用户管理）
│   ├── command_handler.py       # 命令路由（@bot 指令 + / 命令 + 权限校验 + 脏话自动禁言）
│   ├── music.py                 # 音乐核心（搜索、队列、播放、封面缓存、自动切歌）
│   ├── netease.py               # 网易云音乐 API 封装
│   ├── queue_manager.py         # Redis 播放队列管理
│   ├── chat.py                  # AI 聊天 + 图片生成 + 关键词回复 + AI 脏话审核
│   ├── lol_query.py             # LOL 封号查询
│   ├── lol_fa8.py               # LOL 战绩查询（FA8 接口）
│   ├── database.py              # SQLite 数据层（图片缓存、歌曲缓存、播放历史、统计）
│   ├── name_resolver.py         # ID → 名称解析（用户/频道/区域，自动发现 + 持久化）
│   └── logger_config.py         # 日志配置（轮转文件 + 控制台，UTF-8）
│
├── tools/                       # 独立工具
│   ├── credential_tool.py       # 凭据获取工具（RSA 私钥、UID、设备 ID、JWT Token）
│   └── audio_service.py         # 音频播放服务（ffplay + FastAPI）
│
├── data/                        # 运行时数据（自动生成）
│   ├── names.json               # ID → 名称缓存
│   └── oopz_cache.db            # SQLite 数据库文件
│
├── docs/                        # 文档目录
└── logs/                        # 日志文件
```

## 数据库表结构

| 表名 | 用途 | 关键字段 |
|------|------|----------|
| `image_cache` | 封面图片缓存 | source_id, oopz_url, use_count |
| `song_cache` | 歌曲信息缓存 | song_id, song_name, artist, play_count |
| `play_history` | 播放历史记录 | song_cache_id, channel_id, user_id, played_at |
| `statistics` | 每日统计汇总 | date, total_plays, unique_songs, cache_hits |
