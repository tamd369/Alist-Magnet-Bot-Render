import requests
import sys
import re
import logging
import os
import asyncio
import ast
import math
import html
import urllib.parse
from datetime import datetime, timedelta

from dotenv import load_dotenv
from functools import wraps

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# 加载.env 文件中的环境变量
load_dotenv()

# 配置日志（增强过滤规则）
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 禁止httpx的INFO级别日志（过滤HTTP/1.1 200 OK等信息）
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)  # 仅显示WARNING及以上级别日志

# --- 从环境变量加载配置 ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BASE_URL = os.getenv("ALIST_BASE_URL")
USERNAME = os.getenv("ALIST_USERNAME")
PASSWORD = os.getenv("ALIST_PASSWORD")
OFFLINE_DOWNLOAD_DIR = os.getenv("ALIST_OFFLINE_DIR")
SEARCH_URL = os.getenv("JAV_SEARCH_API")
ALLOWED_USER_IDS_STR = os.getenv("ALLOWED_USER_IDS")
# 新增：从.env 文件中加载自动清理间隔时间
CLEAN_INTERVAL_MINUTES = int(os.getenv("CLEAN_INTERVAL_MINUTES", 60))
# 新增：从.env 文件中加载清理阈值
SIZE_THRESHOLD = int(os.getenv("SIZE_THRESHOLD", 100)) * 1024 * 1024

# --- 配置校验 ---
if not all([TELEGRAM_TOKEN, BASE_URL, USERNAME, PASSWORD, OFFLINE_DOWNLOAD_DIR, SEARCH_URL, ALLOWED_USER_IDS_STR]):
    logger.error("错误：环境变量缺失！请检查.env 文件或环境变量设置。")
    sys.exit(1)

try:
    # 将逗号分隔的字符串转换为整数集合
    ALLOWED_USER_IDS = set(map(int, ALLOWED_USER_IDS_STR.split(',')))
    logger.info(f"允许的用户 ID: {ALLOWED_USER_IDS}")
except ValueError:
    logger.error("错误: ALLOWED_USER_IDS 格式不正确，请确保是逗号分隔的数字。")
    sys.exit(1)

# --- 全局配置 ---
TOKEN_EXPIRY_DURATION = timedelta(hours=24)  # Token 有效期 24 小时

# --- 用户授权装饰器 ---
def restricted(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ALLOWED_USER_IDS:
            logger.warning(f"未授权用户尝试访问: {user_id}")
            await update.message.reply_text("抱歉，您没有权限使用此机器人。")
            return
        # 检查并获取 token，存储在 bot_data 中
        token = await get_token(context)
        if not token:
            await update.message.reply_text("错误: 无法连接或登录到 Alist 服务。")
            return
        # 将 token 传递给处理函数
        return await func(update, context, token=token, *args, **kwargs)
    return wrapped

# --- API 函数 ---

def parse_size_to_bytes(size_str: str) -> int | None:
    """Converts size string (e.g., '5.40GB', '1.25MB') to bytes."""
    if not size_str:
        return 0  # Treat empty size as 0 bytes

    size_str = size_str.upper()
    match = re.match(r'^([\d.]+)\s*([KMGTPEZY]?B)$', size_str)
    if not match:
        logger.warning(f"无法解析文件大小: {size_str}")
        return None  # Indicate parsing failure

    value, unit = match.groups()
    try:
        value = float(value)
    except ValueError:
        logger.warning(f"无法解析文件大小值: {value} from {size_str}")
        return None

    unit = unit.upper()
    exponent = 0
    if unit.startswith('K'):
        exponent = 1
    elif unit.startswith('M'):
        exponent = 2
    elif unit.startswith('G'):
        exponent = 3
    elif unit.startswith('T'):
        exponent = 4

    return int(value * (1024 ** exponent))

def parse_api_data_entry(entry_str: str) -> dict | None:
    """Parses a single string entry from the API data list."""
    try:
        data_list = ast.literal_eval(entry_str)
        if not isinstance(data_list, list) or len(data_list) < 4:
            logger.warning(f"解析后的数据格式不正确 (非列表或长度不足): {data_list}")
            return None

        magnet = data_list[0]
        name = data_list[1]
        size_str = data_list[2]
        date_str = data_list[3]

        if not magnet or not magnet.startswith("magnet:?"):
            logger.warning(f"条目中缺少有效的磁力链接: {entry_str}")
            return None

        size_bytes = parse_size_to_bytes(size_str)
        if size_bytes is None:
            logger.warning(f"无法解析大小，跳过条目: {entry_str}")
            return None

        upload_date = None
        try:
            if date_str:
                upload_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            logger.warning(f"无法解析日期 '{date_str}'，日期将为 None")

        return {
            "magnet": magnet,
            "name": name,
            "size_str": size_str,
            "size_bytes": size_bytes,
            "date_str": date_str,
            "date": upload_date,
            "original_string": entry_str
        }

    except (ValueError, SyntaxError, TypeError) as e:
        logger.error(f"解析 API 数据条目时出错: '{entry_str[:100]}...', 错误: {e}")
        return None

def get_magnet(fanhao: str, search_url: str) -> tuple[str | None, str | None]:
    """获取磁力链接（优化版用户提示）"""
    try:
        url = search_url.rstrip('/') + "/" + fanhao
        logger.info(f"正在搜索番号: {fanhao}")
        response = requests.get(url, timeout=20)  # 明确定义 response
        response.raise_for_status()

        raw_result = response.json()

        # --- 处理业务逻辑错误 ---
        if not raw_result or raw_result.get("status") != "succeed":
            error_type = raw_result.get('message', '未知错误')
            if "not found" in error_type.lower():
                return None, f"🔍 未找到番号 {fanhao} 相关资源"
            return None, f"🔍 搜索服务异常 ({error_type[:20]}...)"

        if not raw_result.get("data") or len(raw_result["data"]) == 0:
            return None, f"🔍 番号 {fanhao} 暂无有效磁力"

        # --- 解析数据条目 ---
        parsed_entries = []
        for entry_str in raw_result["data"]:
            parsed = parse_api_data_entry(entry_str)
            if parsed and parsed["magnet"].startswith("magnet:?"):
                parsed_entries.append(parsed)

        if not parsed_entries:
            return None, f"🔍 找到资源但无有效磁力"

        # --- 智能选择逻辑（保持原样）---
        max_size = max(e["size_bytes"] for e in parsed_entries)
        hd_threshold = max_size * 0.7
        selected_cluster = [e for e in parsed_entries if e["size_bytes"] >= hd_threshold] or parsed_entries

        selected_cluster.sort(
            key=lambda x: (x["size_bytes"],
                         -(x["date"].toordinal() if x["date"] else 0)))

        return selected_cluster[0]["magnet"], None

    # --- 异常处理（优化提示）---
    except requests.exceptions.Timeout:
        logger.error(f"搜索超时 ({fanhao})")
        return None, "⏳ 搜索超时，请检查网络连接"

    except requests.exceptions.HTTPError as e:
        # 确保 e.response 存在
        status_code = e.response.status_code  # 正确引用 e.response
        if 500 <= status_code < 600 or status_code == 404:
            return None, f"🔍 番号 {fanhao} 不存在"
        return None, f"🔍 搜索服务异常 (HTTP {status_code})"

    except Exception as e:
        logger.error(f"未知错误 ({fanhao}): {str(e)}", exc_info=True)
        if "timed out" in str(e).lower():
            return None, "⏳ 操作超时，请稍后重试"
        return None, "🔍 搜索时发生意外错误"

async def get_token(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """获取 Alist Token，带有效期缓存"""
    bot_data = context.bot_data
    token = bot_data.get("alist_token")
    token_expiry = bot_data.get("token_expiry")

    if token and token_expiry and datetime.now() < token_expiry:
        logger.info("使用有效缓存的 Alist token")
        return token

    try:
        url = BASE_URL.rstrip('/') + "/api/auth/login"
        logger.info("缓存 token 无效或过期，正在重新获取...")
        login_info = {"username": USERNAME, "password": PASSWORD}
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: requests.post(url, json=login_info, timeout=15)
        )
        response.raise_for_status()

        result = response.json()
        if result.get("code") == 200 and result.get("data") and result["data"].get("token"):
            token = str(result['data']['token'])
            bot_data["alist_token"] = token
            bot_data["token_expiry"] = datetime.now() + TOKEN_EXPIRY_DURATION
            logger.info("成功获取并缓存新的 Alist token")
            return token
        else:
            error_msg = result.get('message', '未知错误')
            logger.error(f"Alist 登录失败: {error_msg} (Code: {result.get('code', 'N/A')})")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"登录 Alist 获取 token 时出错: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"登录 Alist 过程中发生未知错误: {str(e)}", exc_info=True)
        return None

async def add_magnet(context: ContextTypes.DEFAULT_TYPE, token: str, magnet: str) -> tuple[bool, str]:
    """返回格式：(是否成功, 结果描述)"""
    if not token or not magnet:
        logger.error("添加任务失败: token 或磁力链接为空")
        return False, "❌ 内部错误：必要参数缺失"

    try:
        url = BASE_URL.rstrip('/') + "/api/fs/add_offline_download"
        headers = {
            "Authorization": token,
            "Content-Type": "application/json"
        }
        post_data = {
            "path": OFFLINE_DOWNLOAD_DIR,
            "urls": [magnet],
            "tool": "storage",
            "delete_policy": "delete_on_upload_succeed"
        }

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: requests.post(url, json=post_data, headers=headers, timeout=30))

        # 处理已知错误状态
        if response.status_code == 401:
            context.bot_data.pop("alist_token", None)
            return False, "❌ 认证过期，请重试"
        if response.status_code == 500:
            return False, "❌ 服务器拒绝请求（可能重复添加）"

        response.raise_for_status()
        result = response.json()

        if result.get("code") == 200:
            return True, "✅ 已添加至下载队列"
        return False, f"❌ 磁力解析失败"

    except requests.exceptions.Timeout:
        return False, "⏳ 添加超时，请检查网络"
    except requests.exceptions.ConnectionError:
        return False, "🔌 无法连接Alist服务"
    except Exception as e:
        logger.error(f"添加任务异常: {str(e)}")
        return False, f"❌ 意外错误: {str(e)[:50]}"

async def recursive_collect_files(token: str, base_url: str, current_path: str) -> list[str]:
    """递归收集目录下所有小文件（返回绝对路径）"""
    if SIZE_THRESHOLD == 0:
        return []
    list_url = base_url.rstrip('/') + "/api/fs/list"
    headers = {"Authorization": token, "Content-Type": "application/json"}
    payload = {"path": current_path, "page": 1, "per_page": 0}
    files = []

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: requests.post(list_url, json=payload, headers=headers, timeout=20)
        )
        response.raise_for_status()
        list_result = response.json()

        # 防御性数据解析
        data = list_result.get("data") or {}
        if list_result.get("code") != 200:
            logger.error(f"目录列表失败: {list_result.get('message')} (路径: {current_path})")
            return []

        content = data.get("content") or []
        if not isinstance(content, list):
            logger.error(f"无效的API响应格式 (路径: {current_path})")
            return []

        for item in content:
            # 关键路径处理逻辑
            try:
                is_dir = item.get("is_dir", False)
                file_name = item.get("name", "").strip()
                file_size = item.get("size", 0)

                if not file_name:
                    continue

                # 构建标准化绝对路径（兼容Windows/Linux）
                full_path = "/".join([current_path.rstrip("/"), file_name.lstrip("/")])

                if is_dir:
                    # 递归处理目录
                    sub_files = await recursive_collect_files(token, base_url, full_path)
                    files.extend(sub_files)
                else:
                    # 只收集小于阈值文件
                    if file_size < SIZE_THRESHOLD:
                        files.append(full_path)
                        logger.debug(f"找到候选文件: {full_path} ({file_size/1024/1024:.2f} MB)")

            except Exception as e:
                logger.error(f"处理文件项时出错: {str(e)}", exc_info=True)
                continue

        return files

    except requests.exceptions.RequestException as e:
        logger.error(f"网络请求失败: {str(e)} (路径: {current_path})")
        return []
    except Exception as e:
        logger.error(f"未知错误: {str(e)} (路径: {current_path})", exc_info=True)
        return []

async def recursive_collect_empty_dirs(token: str, base_url: str, current_path: str) -> list[str]:
    """递归收集目录下所有空文件夹（返回绝对路径）"""
    list_url = base_url.rstrip('/') + "/api/fs/list"
    headers = {"Authorization": token, "Content-Type": "application/json"}
    payload = {"path": current_path, "page": 1, "per_page": 0}
    empty_dirs = []

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: requests.post(list_url, json=payload, headers=headers, timeout=20)
        )
        response.raise_for_status()
        list_result = response.json()

        # 防御性数据解析
        data = list_result.get("data") or {}
        if list_result.get("code") != 200:
            logger.error(f"目录列表失败: {list_result.get('message')} (路径: {current_path})")
            return []

        content = data.get("content") or []
        if not isinstance(content, list):
            logger.error(f"无效的API响应格式 (路径: {current_path})")
            return []

        sub_dirs = []
        for item in content:
            is_dir = item.get("is_dir", False)
            file_name = item.get("name", "").strip()
            if not file_name:
                continue
            full_path = "/".join([current_path.rstrip("/"), file_name.lstrip("/")])
            if is_dir:
                sub_dirs.append(full_path)
        for sub_dir in sub_dirs:
            sub_empty_dirs = await recursive_collect_empty_dirs(token, base_url, sub_dir)
            empty_dirs.extend(sub_empty_dirs)

        if not sub_dirs and not any(not item.get("is_dir", False) for item in content):
            empty_dirs.append(current_path)

        return empty_dirs

    except requests.exceptions.RequestException as e:
        logger.error(f"网络请求失败: {str(e)} (路径: {current_path})")
        return []
    except Exception as e:
        logger.error(f"未知错误: {str(e)} (路径: {current_path})", exc_info=True)
        return []


async def cleanup_empty_dirs(token: str, base_url: str, target_dir: str) -> tuple[int, str]:
    """清理空文件夹并返回成功删除的文件夹数+结果信息"""
    try:
        empty_dirs = await recursive_collect_empty_dirs(token, base_url, target_dir)
        if not empty_dirs:
            return 0, "✅ 未找到空文件夹"

        total_deleted = 0
        error_messages = []
        for dir_path in empty_dirs:
            try:
                remove_url = base_url.rstrip('/') + "/api/fs/remove"
                headers = {"Authorization": token, "Content-Type": "application/json"}
                delete_payload = {
                    "dir": os.path.dirname(dir_path),
                    "names": [os.path.basename(dir_path)]
                }
                response = requests.post(remove_url, json=delete_payload, headers=headers, timeout=30)
                if response.status_code == 200:
                    result = response.json()
                    if result.get("code") == 200:
                        total_deleted += 1
                        logger.debug(f"成功删除空文件夹: {dir_path}")
                    else:
                        error_msg = result.get("message", "未知错误")
                        error_messages.append(f"文件夹 {os.path.basename(dir_path)}: {error_msg}")
                else:
                    response.raise_for_status()
            except requests.exceptions.RequestException as e:
                error_messages.append(f"文件夹 {os.path.basename(dir_path)}: 网络错误")
            except Exception as e:
                error_messages.append(f"文件夹 {os.path.basename(dir_path)}: {str(e)}")

        if error_messages:
            return total_deleted, (
                f"❌ 部分删除失败 (成功 {total_deleted} 文件夹)\n"
                f"错误({min(len(error_messages), 3)}/{len(error_messages)}):\n" +
                "\n".join([f"• {msg}" for msg in error_messages[:3]])
            )

        return total_deleted, f"✅ 成功删除 {total_deleted} 个空文件夹"

    except Exception as e:
        logger.error(f"清理空文件夹异常: {str(e)}", exc_info=True)
        return 0, f"❌ 系统错误: {str(e)}"


async def cleanup_small_files(token: str, base_url: str, target_dir: str) -> tuple[int, str]:
    if SIZE_THRESHOLD == 0:
        return 0, "✅ 小文件清理功能未启用"
    try:
        from collections import defaultdict
        import os
        from urllib.parse import quote

        logger.info(f"开始清理目录: {target_dir}")
        files_to_delete = await recursive_collect_files(token, base_url, target_dir)

        if not files_to_delete:
            return 0, "✅ 未找到小于指定大小的文件"

        # 按父目录分组文件
        dir_files = defaultdict(list)
        for abs_path in files_to_delete:
            parent_dir = os.path.dirname(abs_path)
            file_name = os.path.basename(abs_path)
            dir_files[parent_dir].append(file_name)

        total_deleted_files = 0
        file_error_messages = []

        # 分目录批量删除
        for parent_dir, file_names in dir_files.items():
            try:
                remove_url = base_url.rstrip('/') + "/api/fs/remove"
                headers = {"Authorization": token, "Content-Type": "application/json"}

                # URL编码处理（兼容特殊字符）
                encoded_dir = quote(parent_dir, safe='')
                encoded_names = [quote(name, safe='') for name in file_names]

                delete_payload = {
                    "dir": parent_dir,
                    "names": file_names
                }

                response = requests.post(remove_url, json=delete_payload, headers=headers, timeout=30)

                if response.status_code == 200:
                    result = response.json()
                    if result.get("code") == 200:
                        deleted = len(file_names)
                        total_deleted_files += deleted
                        logger.debug(f"成功删除 {deleted} 个文件于 {parent_dir}")
                    else:
                        # 记录更详细的错误信息
                        error_msg = f"目录 {os.path.basename(parent_dir)}: API 返回错误码 {result.get('code')}，消息: {result.get('message')}"
                        file_error_messages.append(error_msg)
                else:
                    response.raise_for_status()

            except requests.exceptions.RequestException as e:
                error_msg = f"目录 {os.path.basename(parent_dir)}: 网络错误 - {str(e)}"
                file_error_messages.append(error_msg)
            except Exception as e:
                error_msg = f"目录 {os.path.basename(parent_dir)}: {str(e)}"
                file_error_messages.append(error_msg)

        # 清理完小文件后，继续清理空文件夹
        total_deleted_dirs, dir_msg = await cleanup_empty_dirs(token, base_url, target_dir)

        # 生成结果信息
        if file_error_messages and total_deleted_files == 0 and total_deleted_dirs == 0:
            return 0, (
                f"❌ 部分删除失败 (成功 0 个文件和 0 个空文件夹)\n"
                f"错误({min(len(file_error_messages), 3)}/{len(file_error_messages)}):\n" +
                "\n".join([f"• {msg}" for msg in file_error_messages[:3]])
            )

        if total_deleted_files > 0 or total_deleted_dirs > 0:
            if total_deleted_dirs > 0:
                if file_error_messages:
                    return total_deleted_files, (
                        f"✅ 部分文件删除失败，但成功删除 {total_deleted_files} 个文件和 {total_deleted_dirs} 个空文件夹\n"
                        f"文件删除错误({min(len(file_error_messages), 3)}/{len(file_error_messages)}):\n" +
                        "\n".join([f"• {msg}" for msg in file_error_messages[:3]])
                    )
                else:
                    return total_deleted_files, f"✅ 成功删除 {total_deleted_files} 个文件和 {total_deleted_dirs} 个空文件夹"
            else:
                if file_error_messages:
                    return total_deleted_files, (
                        f"✅ 部分文件删除失败，但成功删除 {total_deleted_files} 个文件\n"
                        f"文件删除错误({min(len(file_error_messages), 3)}/{len(file_error_messages)}):\n" +
                        "\n".join([f"• {msg}" for msg in file_error_messages[:3]])
                    )
                else:
                    return total_deleted_files, f"✅ 成功删除 {total_deleted_files} 个文件。{dir_msg}"
        else:
            return 0, f"✅ 未找到小于指定大小的文件，{dir_msg}"

    except Exception as e:
        logger.error(f"清理异常: {str(e)}", exc_info=True)
        return 0, f"❌ 系统错误: {str(e)}"


async def find_download_directory(token: str, base_url: str, parent_dir: str, original_code: str) -> tuple[list[str] | None, str | None]:
    """返回所有匹配的目录列表"""
    logger.info(f"在目录 '{parent_dir}' 中搜索番号 '{original_code}'...")
    list_url = base_url.rstrip('/') + "/api/fs/list"
    headers = {"Authorization": token, "Content-Type": "application/json"}

    try:
        # 路径标准化处理
        parent_dir = parent_dir.strip().replace('\\', '/').rstrip('/')
        if not parent_dir.startswith('/'):
            parent_dir = f'/{parent_dir}'

        list_payload = {"path": parent_dir, "page": 1, "per_page": 0}
        response = requests.post(list_url, json=list_payload, headers=headers, timeout=20)
        response.raise_for_status()
        list_result = response.json()

        if list_result.get("code") != 200:
            return None, f"目录列表失败: {list_result.get('message', '未知错误')}"

        content = list_result.get("data", {}).get("content", [])
        target_pattern = re.sub(r'[^a-zA-Z0-9]', '', original_code).lower()
        possible_matches = []

        for item in content:
            if item.get("is_dir"):
                dir_name = item.get("name", "").strip()
                normalized_dir = re.sub(r'[^a-zA-Z0-9]', '', dir_name).lower()
                if normalized_dir.startswith(target_pattern):
                    full_path = f"{parent_dir.rstrip('/')}/{dir_name}".replace('//', '/')
                    possible_matches.append(full_path)
                    logger.debug(f"找到候选目录: {full_path}")

        return possible_matches, None

    except Exception as e:
        logger.error(f"目录搜索异常: {str(e)}")
        return None, f"目录搜索失败: {str(e)}"


# --- Telegram 命令处理函数 ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("抱歉，您没有权限使用此机器人。")
        return
    await update.message.reply_text(
        '欢迎使用 JAV 下载机器人！\n'
        '直接发送番号（如 ABC-123）或磁力链接，我会帮你添加到 Alist 离线下载。\n'
        '/help 查看帮助。'
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("抱歉，您没有权限使用此机器人。")
        return
    await update.message.reply_text(
        '使用方法：\n'
        '1. 直接发送番号（例如：`ABC-123`, `IPX-888`）\n'
        '2. 直接发送磁力链接（以 `magnet:?` 开头）\n\n'
        '3. 清理功能：\n'
        '   - `/clean <番号>` 清理该番号对应的下载目录\n'
        '   - `/clean /` 递归清理所有下载目录（谨慎使用！）\n\n'
        '4. 刷新功能：\n'
        '   - `/refresh` 刷新 Alist 文件列表\n\n'
        f'当前配置的下载根目录: `{OFFLINE_DOWNLOAD_DIR}`',
        parse_mode='Markdown'
    )


FANHAO_REGEX = re.compile(
    r'^[A-Za-z]{2,5}[-_ ]?\d{2,5}(?:[-_ ]?[A-Za-z])?$',
    re.IGNORECASE
)


# 新增：处理单条输入的函数
async def handle_single_entry(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str, entry: str):
    chat_id = update.effective_chat.id
    loop = asyncio.get_event_loop()
    processing_msg = None

    try:
        if entry.startswith("magnet:?"):
            logger.info(f"收到磁力链接: {entry[:50]}...")
            processing_msg = await update.message.reply_text("🔗 收到磁力链接，准备添加...")
            success, result_msg = await add_magnet(context, token, entry)
        elif FANHAO_REGEX.match(entry):
            logger.info(f"收到可能的番号: {entry}")
            processing_msg = await update.message.reply_text(f"🔍 正在搜索番号: {entry}...")
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

            # 同步函数转异步执行
            magnet, error_msg = await loop.run_in_executor(
                None, lambda: get_magnet(entry, SEARCH_URL)
            )

            if not magnet:
                await processing_msg.edit_text(f"❌ 搜索失败: {error_msg}")
                return

            await processing_msg.edit_text(f"✅ 已找到磁力链接，正在添加到 Alist...")
            success, result_msg = await add_magnet(context, token, magnet)
        else:
            await update.message.reply_text("无法识别的消息格式。请发送番号（如 ABC-123）或磁力链接。")
            return

        if processing_msg:
            await processing_msg.edit_text(result_msg)
        else:
            await update.message.reply_text(result_msg)

        if success:
            await asyncio.sleep(3)
            await refresh_command(update, context)

    except Exception as e:
        logger.error(f"处理异常: {str(e)}", exc_info=True)
        error_msg = f"❌ 处理失败: {str(e)[:100]}"
        if processing_msg:
            await processing_msg.edit_text(error_msg)
        else:
            await update.message.reply_text(error_msg)


# 新增：处理批量输入的函数
async def handle_batch_entries(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str, entries: list[str]):
    chat_id = update.effective_chat.id
    loop = asyncio.get_running_loop()
    progress_msg = await update.message.reply_text(f"🔄 开始批量处理 {len(entries)} 个任务...")
    results = []
    BATCH_DELAY = 0.8

    for idx, entry in enumerate(entries, 1):
        try:
            # 更新进度消息
            success_count = sum(1 for res in results if res[1])
            await progress_msg.edit_text(
                f"⏳ 处理进度: {idx}/{len(entries)}\n"
                f"当前项: {entry[:15]}...\n"
                f"成功: {success_count} 失败: {len(results)-success_count}"
            )

            # 处理逻辑
            if entry.startswith("magnet:?"):
                success, msg = await add_magnet(context, token, entry)
                results.append((entry, success, msg))
            elif FANHAO_REGEX.match(entry):
                magnet, error = await loop.run_in_executor(None, lambda: get_magnet(entry, SEARCH_URL))
                if magnet:
                    success, msg = await add_magnet(context, token, magnet)
                    results.append((entry, success, msg))
                else:
                    results.append((entry, False, f"搜索失败: {error}"))
            else:
                results.append((entry, False, "格式错误"))

            await asyncio.sleep(BATCH_DELAY)

        except Exception as e:
            logger.error(f"批量处理异常: {entry} - {str(e)}")
            results.append((entry, False, f"处理异常: {str(e)[:50]}"))
            await asyncio.sleep(BATCH_DELAY * 2)

    # 生成统计报告
    success_count = sum(1 for res in results if res[1])
    report = [
        f"✅ 批量处理完成 ({success_count}/{len(entries)})",
        "━━━━━━━━━━━━━━━",
        *[f"{'🟢' if res[1] else '🔴'} {res[0][:20]}... | {res[2][:30]}"
          for res in results[:10]],  # 显示前10条结果
        "━━━━━━━━━━━━━━━",
        f"成功: {success_count} 条 | 失败: {len(entries)-success_count} 条"
    ]
    if len(results) > 10:
        report.insert(3, f"（仅显示前10条结果，共{len(entries)}条）")

    await progress_msg.edit_text("\n".join(report))
    await context.bot.send_message(
        chat_id=chat_id,
        text="💡 提示：使用 /clean / 命令可以清理所有垃圾文件",
        reply_to_message_id=progress_msg.message_id
    )

    if success_count > 0:
        await asyncio.sleep(3)
        await refresh_command(update, context)


@restricted
async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str) -> None:
    message_text = update.message.text.strip()
    # 分割多行输入并过滤空行
    entries = [line.strip() for line in message_text.split('\n') if line.strip()]
    if not entries:
        await update.message.reply_text("⚠️ 输入内容为空，请发送番号或磁力链接。")
        return

    if len(entries) == 1:
        await handle_single_entry(update, context, token, entries[0])
    else:
        await handle_batch_entries(update, context, token, entries)


@restricted
async def clean_command(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str) -> None:
    """自动清理所有匹配目录（带实时进度）"""
    if SIZE_THRESHOLD == 0:
        await update.message.reply_text("✅ 小文件清理功能未启用")
        return
    if not context.args:
        await update.message.reply_text("请提供清理参数：/clean <番号> 或 /clean /")
        return

    target = context.args[0].strip()
    chat_id = update.effective_chat.id
    processing_msg = await update.message.reply_text(f"🧹 开始清理任务（目标: {target}）...")

    try:
        if target == "/":
            # 全目录清理逻辑
            deleted_files, msg = await cleanup_small_files(token, BASE_URL, OFFLINE_DOWNLOAD_DIR)
            final_text = f"全局清理完成\n{msg}"
            await processing_msg.edit_text(final_text)
            return

        # 获取所有匹配目录
        directories, find_error = await find_download_directory(token, BASE_URL, OFFLINE_DOWNLOAD_DIR, target)
        if not directories:
            await processing_msg.edit_text(f"❌ 清理失败: {find_error}")
            return

        logger.info(f"找到 {len(directories)} 个匹配目录，开始批量清理...")

        success_dirs = 0
        total_files = 0
        total_dirs = len(directories)
        error_messages = []

        # 清理所有匹配目录
        for idx, dir_path in enumerate(directories, 1):
            await processing_msg.edit_text(
                f"🧹 正在清理 ({idx}/{total_dirs}): {os.path.basename(dir_path)}..."
            )
            deleted, msg = await cleanup_small_files(token, BASE_URL, dir_path)
            if deleted > 0:
                success_dirs += 1
                total_files += deleted
            if '❌' in msg:
                error_messages.append(msg)

        # 生成最终报告
        zero_dirs_count = total_dirs - success_dirs - len(error_messages)
        if success_dirs > 0 and zero_dirs_count == 0 and len(error_messages) == 0:
            final_text = (
                f"✅ 清理完成！共清理 {total_files} 个小文件，涉及 {success_dirs} 个目录。"
            )
        elif success_dirs > 0 and zero_dirs_count == 0 and len(error_messages) > 0:
            final_text = (
                f"✅ 部分清理完成！成功清理 {total_files} 个小文件，涉及 {success_dirs} 个目录。\n"
                f"❌ 以下目录清理失败 ({len(error_messages)}):\n" +
                "\n".join([f"• {msg}" for msg in error_messages[:3]])
            )
        elif success_dirs > 0 and zero_dirs_count > 0 and len(error_messages) == 0:
            final_text = (
                f"✅ 部分清理完成！成功清理 {total_files} 个小文件，涉及 {success_dirs} 个目录。\n"
                f"⚠️ 以下目录未找到需要清理的文件 ({zero_dirs_count}):\n" +
                "\n".join([os.path.basename(d) for d in directories if d not in [d for _, msg in zip(directories, msg) if '✅' in msg]])
            )
        elif success_dirs > 0 and zero_dirs_count > 0 and len(error_messages) > 0:
            final_text = (
                f"✅ 部分清理完成！成功清理 {total_files} 个小文件，涉及 {success_dirs} 个目录。\n"
                f"⚠️ 以下目录未找到需要清理的文件 ({zero_dirs_count}):\n" +
                "\n".join([os.path.basename(d) for d in directories if d not in [d for _, msg in zip(directories, msg) if '✅' in msg]]) +
                f"\n❌ 以下目录清理失败 ({len(error_messages)}):\n" +
                "\n".join([f"• {msg}" for msg in error_messages[:3]])
            )
        elif success_dirs == 0 and zero_dirs_count == 0 and len(error_messages) > 0:
            final_text = (
                f"❌ 清理失败！未成功清理任何目录。\n" +
                "\n".join([f"• {msg}" for msg in error_messages[:3]])
            )
        elif success_dirs == 0 and zero_dirs_count > 0 and len(error_messages) == 0:
            final_text = (
                f"⚠️ 所有目录均未找到需要清理的文件 ({total_dirs})。"
            )
        elif success_dirs == 0 and zero_dirs_count > 0 and len(error_messages) > 0:
            final_text = (
                f"❌ 清理失败！未成功清理任何目录。\n"
                f"⚠️ 以下目录未找到需要清理的文件 ({zero_dirs_count}):\n" +
                "\n".join([os.path.basename(d) for d in directories if d not in [d for _, msg in zip(directories, msg) if '✅' in msg]]) +
                f"\n❌ 以下目录清理失败 ({len(error_messages)}):\n" +
                "\n".join([f"• {msg}" for msg in error_messages[:3]])
            )
        else:
            final_text = (
                f"✅ 部分清理完成！成功清理 {total_files} 个小文件，涉及 {success_dirs} 个目录。"
            )

        await processing_msg.edit_text(final_text)

    except Exception as e:
        logger.error(f"清理命令异常: {str(e)}", exc_info=True)
        await processing_msg.edit_text(f"❌ 清理过程中出现未知错误: {str(e)[:50]}")


@restricted
async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE, *, token: str) -> None:
    """发送刷新请求以刷新 Alist"""
    refresh_url = BASE_URL.rstrip('/') + "/api/fs/list"
    headers = {
        "Authorization": token,
        "Content-Type": "application/json"
    }
    payload = {"path": "/", "page": 1, "per_page": 0}
    chat_id = update.effective_chat.id
    processing_msg = await update.message.reply_text("🔄 正在刷新 Alist...")

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: requests.post(refresh_url, json=payload, headers=headers, timeout=30)
        )
        response.raise_for_status()
        result = response.json()

        if result.get("code") == 200:
            await processing_msg.edit_text("✅ Alist 刷新成功！")
        else:
            error_msg = result.get("message", "未知错误")
            await processing_msg.edit_text(f"❌ 刷新失败: {error_msg}")

    except requests.exceptions.RequestException as e:
        logger.error(f"刷新 Alist 时出错: {str(e)}")
        await processing_msg.edit_text(f"❌ 刷新失败: 网络错误 ({str(e)[:50]})")
    except Exception as e:
        logger.error(f"刷新 Alist 时发生未知错误: {str(e)}", exc_info=True)
        await processing_msg.edit_text(f"❌ 刷新失败: 未知错误 ({str(e)[:50]})")


# --- 自动清理定时任务 ---
async def auto_clean(context: ContextTypes.DEFAULT_TYPE):
    if CLEAN_INTERVAL_MINUTES == 0 or SIZE_THRESHOLD == 0:
        logger.info("自动清理任务未启用")
        return
    token = await get_token(context)
    if not token:
        logger.error("无法获取 Alist token，自动清理任务失败。")
        return

    chat_id = list(ALLOWED_USER_IDS)[0]  # 假设使用第一个允许的用户 ID 发送结果
    processing_msg = await context.bot.send_message(chat_id=chat_id, text="🧹 开始自动清理任务...")

    try:
        deleted_files, msg = await cleanup_small_files(token, BASE_URL, OFFLINE_DOWNLOAD_DIR)
        final_text = f"自动清理完成\n{msg}"
        await processing_msg.edit_text(final_text)
    except Exception as e:
        logger.error(f"自动清理任务异常: {str(e)}", exc_info=True)
        error_text = [
            "❌ 自动清理过程发生严重错误",
            f"错误类型: {type(e).__name__}",
            f"详细信息: {str(e)}"
        ]
        await processing_msg.edit_text("\n".join(error_text))


# --- 主函数 ---
def main() -> None:
    """启动机器人"""
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # 注册命令处理程序
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("clean", clean_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_message))

    # 启动自动清理任务
    job_queue = application.job_queue
    job_queue.run_repeating(auto_clean, interval=CLEAN_INTERVAL_MINUTES * 60, first=0)

    # 启动机器人
    application.run_polling()


if __name__ == "__main__":
    main()
