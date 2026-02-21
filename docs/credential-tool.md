# 凭据获取工具

`tools/credential_tool.py` 用于自动从 Oopz 网页端提取 Bot 运行所需的全部凭据。

## 安装

```shell
# 安装 Python 依赖（含 playwright）
pip install -r requirements.txt

# 首次使用需安装 Chromium 浏览器内核
python -m playwright install chromium
```

## 运行

```shell
# 交互模式：捕获后询问是否保存
python tools/credential_tool.py

# 自动保存模式：捕获后直接写入配置文件
python tools/credential_tool.py --save
```

## 工作原理

1. 工具启动 Chromium 浏览器并打开 `https://web.oopz.cn`
2. 用户在浏览器中登录 Oopz 账号（使用持久化目录，下次无需重复登录）
3. 工具通过以下方式自动捕获凭据：

| 凭据 | 捕获方式 |
|------|----------|
| **用户 UID** (`person_uid`) | 拦截 HTTP 请求头 `Oopz-Person` / WebSocket 认证帧 |
| **设备 ID** (`device_id`) | 拦截 HTTP 请求头 `Oopz-Device-Id` / WebSocket 认证帧 |
| **JWT Token** (`jwt_token`) | 拦截 HTTP 请求头 `Oopz-Signature` / WebSocket 认证帧 |
| **RSA 私钥** | 注入 Web Crypto API 钩子，在密钥 import/generate/sign 时强制导出为 PKCS#8 PEM |

4. 捕获完成后，凭据保存到：
   - `config.py` — 写入 `person_uid`、`device_id`、`jwt_token`
   - `private_key.py` — 写入 RSA 私钥 PEM
   - `data/credentials.txt` — 所有凭据的纯文本备份

## RSA 私钥提取说明

Oopz 网页端使用 Web Crypto API 生成 RSA 密钥对，默认以不可导出的 `CryptoKey` 对象存储在 IndexedDB 中。

工具通过在页面加载前注入脚本，覆写 `SubtleCrypto.prototype` 的以下方法：

- `importKey` — 强制 `extractable = true`，密钥导入时自动导出 PEM
- `generateKey` — 强制 `extractable = true`，密钥生成时自动导出 PEM
- `sign` — 在首次签名调用时尝试导出密钥

从而在密钥生成或首次签名时自动导出为 PKCS#8 PEM 格式。

### 首次未能捕获的处理

如果密钥已缓存为不可导出的 `CryptoKey`（直接从 IndexedDB 反序列化，不经过 `importKey`），工具会提示清除 IndexedDB 缓存并刷新页面，迫使浏览器重新生成密钥，此时钩子即可拦截。

## 注意事项

- 浏览器数据保存在 `.oopz_capture_credentials/` 目录（已 gitignore），登录状态可跨次复用
- 生成的 `config.py`、`private_key.py`、`data/credentials.txt` 均已在 `.gitignore` 中排除，不会被提交到 Git
