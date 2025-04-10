# Alist-Magnet-Bot-Render

一个通过 Telegram 控制 Alist 离线下载的磁力搜索与推送机器人。

本项目基于群友 Patty 的 Python 脚本，由群友 misaka 增强了垃圾清理功能，群友 0721 用 Go 语言重构并提供了适用于 Render 的镜像。

---

## 🚀 功能简介
- 支持 Telegram 控制磁力链接搜索与推送到 Alist 离线下载目录
- 可对接任意公开搜索 API（默认已配置）
- 支持垃圾文件清理指令（如 `/clean`）

---

## ☁️ Render 部署 ThunderXBot 教程

### 步骤 1：注册 / 登录 Render
- 打开 [https://render.com](https://render.com)
- 使用 GitHub 账号或邮箱注册 / 登录

---

### 步骤 2：创建 Web Service

1. 登录后点击 `New` → `Web Service`
2. 选择 `Existing Image`
3. 填写以下信息：

| 项目         | 内容 |
|--------------|------|
| **Docker Image** | `node8848/thunderxbot-for-render:latest` |
| **Service Name** | 自定义，例如 `thunderxbot` |
| **Region**       | 建议选择靠近你用户的区域，如 `Singapore` |

---

### 步骤 3：配置环境变量

在 `Environment Variables` 一栏填入以下内容：

| 环境变量名 | 示例值 | 说明 |
|------------|--------|------|
| `BOT_BASE_URL` | `http://127.0.0.1:5244` | Alist 的主页地址 |
| `BOT_SEARCH_URL` | `https://api.wwlww.org/v1/avcode/` | 用于搜索磁力链接的 API 地址 |
| `BOT_USERNAME` | `alist用户名` | Alist 登录用户名 |
| `BOT_PASSWORD` | `alist密码` | Alist 登录密码 |
| `BOT_OFFLINE_DOWNLOAD_DIR` | `/thunderx` | Alist 设置的离线下载保存路径 |
| `BOT_TELEGRAM_TOKEN` | `Telegram Bot 的 Token` | 在 @BotFather 创建 Bot 后获得 |

✅ 填写完毕点击 `Deploy` 即可部署。

📌 **如果提示绑定信用卡：**
使用干净的美国家宽节点全局代理，我实测一次成功。

---

### 步骤 4：设置保活（可选）

Render 免费服务有休眠限制，可以通过第三方保活项目实现持续运行。

推荐项目：[Auto-keep-online by eooce](https://github.com/eooce/Auto-keep-online)

👉 部署后访问项目分配给你的域名，即可自动保持 Render 服务在线。

uptime,哪吒面板等工具增加监控任务也可以

---

## 🐳 Docker 通用部署方式

群友 0721 提供了适用于任意 Docker 平台的镜像：

```bash
docker run -d --restart=unless-stopped \
  -v /opt/thunderx_bot:/app \
  --name="thunderxbot" \
  node8848/thunderxbot:latest

运行容器后，编辑配置文件
`/opt/thunderx_bot/config.json`：

```bash
nano /opt/thunderx_bot/config.json

填入你的配置信息后，重启容器以生效：

`docker restart thunderxbot`
 其他平台部署说明
如果你在 Render 上无法成功部署：

可以在 Releases 中下载适用于其他平台（如 Minecraft 容器、Python 运行环境）的版本
压缩包內说明文件

🙏 致谢
感谢以下群友对本项目的贡献与支持：

- [群友Patty](https://t.me/joinchat/GZxTslH80phQbAR0bglMMA)提供原始 Python 脚本

misaka：增强 /alean 指令，实现垃圾文件清理功能

0721：用 Go 语言重写项目，并提供 Render 镜像与多平台 Docker 镜像支持
- [eooce](https://github.com/eooce)

