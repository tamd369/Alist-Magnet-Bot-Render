import os
import sys
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from flask import Flask
import requests
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()  # 确保从 .env 文件加载环境变量

# 配置日志
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 配置信息
username = os.getenv("USERNAME")
password = os.getenv("PASSWORD")
base_url = os.getenv("BASE_URL")
offline_download_dir = os.getenv("OFFLINE_DOWNLOAD_DIR")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
search_url = os.getenv("SEARCH_URL", "https://api.wwlww.org/v1/avcode/")

# 全局token缓存
global_token = None

# Flask Web 服务
app = Flask(__name__)

@app.route('/')
def home():
    return 'Web 服务正在运行！你可以通过 Telegram 机器人与我交互。'

@app.route('/health')
def health_check():
    return 'OK', 200

# 获取磁力链接
def get_magnet(fanhao, search_url):
    try:
        url = search_url + fanhao
        logger.info(f"正在搜索番号: {fanhao}...")
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        if not data.get("data") or len(data["data"]) == 0:
            logger.error(f"错误: 未找到番号 {fanhao} 的磁力链接")
            return None
            
        first_entry = data["data"][0]
        magnet = first_entry.split(",")[0].strip("['")
        logger.info(f"成功获取磁力链接: {magnet}")
        return magnet
    except requests.exceptions.RequestException as e:
        logger.error(f"获取磁力链接时出错: {str(e)}")
        return None
    except (KeyError, IndexError) as e:
        logger.error(f"解析磁力链接数据时出错: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"获取磁力链接时发生未知错误: {str(e)}")
        return None

def get_token(username, password, base_url):
    global global_token
    if global_token:
        return global_token
        
    try:
        url = base_url + "api/auth/login"
        logger.info("正在登录获取token...")
        login_info = {
            "username": username,
            "password": password
        }
        response = requests.post(url, json=login_info, timeout=10)
        response.raise_for_status()
        
        result = response.json()
        if not result.get("data") or not result["data"].get("token"):
            logger.error(f"登录失败: {result.get('message', '未知错误')}")
            return None
            
        token = str(result['data']['token'])
        logger.info("登录成功，已获取token")
        global_token = token
        return token
    except requests.exceptions.RequestException as e:
        logger.error(f"登录获取token时出错: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"登录过程中发生未知错误: {str(e)}")
        return None

def add_magnet(base_url, token, offline_download_dir, magnet):
    if not token or not magnet:
        logger.error("错误: token或磁力链接为空，无法添加离线下载任务")
        return False
        
    try:
        url = base_url + "api/fs/add_offline_download"
        logger.info(f"正在添加离线下载任务到目录: {offline_download_dir}")
        
        headers = {
            "Authorization": token,
            "Content-Type": "application/json"
        }
        post_data = {
            "path": offline_download_dir,
            "urls": [magnet],
            "tool": "storage",
            "delete_policy": "delete_on_upload_succeed"
        }
        
        response = requests.post(url, json=post_data, headers=headers, timeout=10)
        response.raise_for_status()
        
        result = response.json()
        if result.get("code") == 200:
            logger.info("离线下载任务添加成功!")
            return True
        else:
            logger.error(f"添加离线下载任务失败: {result.get('message', '未知错误')}")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"添加离线下载任务时出错: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"添加离线下载任务时发生未知错误: {str(e)}")
        return False

# Telegram机器人命令处理函数
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        '欢迎使用JAV下载机器人！\n'
        '直接发送番号或磁力链接，我会帮你添加到离线下载队列。'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        '使用方法：\n'
        '1. 直接发送番号（如：ABC-123）\n'
        '2. 直接发送磁力链接（以magnet:?开头）\n'
        '机器人会自动处理并添加到离线下载队列。'
    )

async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message_text = update.message.text.strip()
    
    # 获取token
    token = get_token(username, password, base_url)
    if not token:
        await update.message.reply_text("错误: 获取token失败，无法处理请求")
        return
    
    # 判断是番号还是磁力链接
    if message_text.startswith("magnet:?"):
        magnet = message_text
        await update.message.reply_text(f"收到磁力链接，正在添加到离线下载队列...")
    else:
        await update.message.reply_text(f"正在搜索番号: {message_text}...")
        magnet = get_magnet(message_text, search_url)
        if not magnet:
            await update.message.reply_text(f"错误: 未找到番号 {message_text} 的磁力链接")
            return
        await update.message.reply_text(f"已找到磁力链接，正在添加到离线下载队列...")
    
    # 添加离线下载任务
    success = add_magnet(base_url, token, offline_download_dir, magnet)
    if success:
        await update.message.reply_text("✅ 离线下载任务添加成功！")
    else:
        await update.message.reply_text("❌ 添加离线下载任务失败")

def main() -> None:
    """启动 Telegram 机器人"""
    # 创建应用
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # 添加处理程序
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_message))

    # 启动 Telegram 机器人
    logger.info("启动Telegram机器人...")
    application.run_polling()

if __name__ == "__main__":
    try:
        # 启动 Telegram 机器人和 Web 服务
        from threading import Thread

        # 启动 Flask Web 服务
        def start_flask():
            app.run(host='0.0.0.0', port=5000)
        
        flask_thread = Thread(target=start_flask)
        flask_thread.start()

        # 启动 Telegram 机器人
        main()
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
        sys.exit(0)
    except Exception as e:
        logger.error(f"程序执行过程中发生未知错误: {str(e)}")
        sys.exit(1)
