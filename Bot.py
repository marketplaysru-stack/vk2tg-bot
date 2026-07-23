import asyncio
import logging
import os
import sys
import json
import time
import requests
import re
from datetime import datetime, timedelta

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ===== НАСТРОЙКА ЛОГИРОВАНИЯ =====
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== ЗАГРУЗКА ТОКЕНА =====
MAIN_TOKEN = os.getenv("TG_MAIN_TOKEN")
if not MAIN_TOKEN:
    logger.error("❌ TG_MAIN_TOKEN не задан в переменных окружения!")
    sys.exit(1)
logger.info("✅ Токен загружен")

# ===== ПУТЬ К ФАЙЛУ КОНФИГУРАЦИИ =====
DATA_DIR = "/data"
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = "."
CONFIG_FILE = os.path.join(DATA_DIR, "vk2tg_config.json")
logger.info(f"📂 Файл конфигурации: {CONFIG_FILE}")

# ===== КОНСТАНТЫ =====
MAX_POSTS_PER_DAY = 4
MAX_CONFIGS_PER_USER = 10
SEND_DELAY = 10  # пауза между отправками в секундах
MAX_MESSAGE_LENGTH = 4000

# ===== ЗАГРУЗКА / СОХРАНЕНИЕ КОНФИГУРАЦИИ =====
def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

# ===== РАБОТА С КОНФИГУРАЦИЯМИ =====
def get_user_configs(user_id):
    config = load_config()
    return config.get(str(user_id), [])

def add_user_config(user_id, vk_token, vk_group_id, tg_token, tg_chat_id):
    config = load_config()
    key = str(user_id)
    if key not in config:
        config[key] = []
    for entry in config[key]:
        if entry["vk_group_id"] == vk_group_id:
            return False
    config[key].append({
        "vk_token": vk_token,
        "vk_group_id": vk_group_id,
        "tg_token": tg_token,
        "tg_chat_id": tg_chat_id,
        "last_post_id": 0,
        "enabled": True,
        "daily_posts": []
    })
    save_config(config)
    return True

def delete_user_config(user_id, vk_group_id):
    config = load_config()
    key = str(user_id)
    if key in config:
        config[key] = [e for e in config[key] if e["vk_group_id"] != vk_group_id]
        if not config[key]:
            del config[key]
        save_config(config)
        return True
    return False

def toggle_enable(user_id, vk_group_id, enabled):
    config = load_config()
    key = str(user_id)
    if key in config:
        for entry in config[key]:
            if entry["vk_group_id"] == vk_group_id:
                entry["enabled"] = enabled
                save_config(config)
                return True
    return False

def update_last_post_id(user_id, vk_group_id, post_id):
    config = load_config()
    key = str(user_id)
    if key in config:
        for entry in config[key]:
            if entry["vk_group_id"] == vk_group_id:
                entry["last_post_id"] = post_id
                save_config(config)
                logger.info(f"   📝 Обновлён last_post_id для группы {vk_group_id}: {post_id}")
                return True
    return False

def mark_post_sent(user_id, vk_group_id, post_id):
    config = load_config()
    key = str(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    if key in config:
        for entry in config[key]:
            if entry["vk_group_id"] == vk_group_id:
                entry["daily_posts"] = [p for p in entry["daily_posts"] if p.get("date") == today]
                entry["daily_posts"].append({"post_id": post_id, "date": today})
                save_config(config)
                logger.info(f"   📌 Отмечен как отправленный сегодня: пост {post_id}")
                return True
    return False

def get_today_post_count(user_id, vk_group_id):
    config = load_config()
    key = str(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    if key in config:
        for entry in config[key]:
            if entry["vk_group_id"] == vk_group_id:
                count = sum(1 for p in entry["daily_posts"] if p.get("date") == today)
                logger.info(f"   📊 Сегодня отправлено {count} постов для группы {vk_group_id}")
                return count
    return 0

def clear_old_daily_posts():
    config = load_config()
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    changed = False
    for user_id, entries in config.items():
        for entry in entries:
            old_len = len(entry["daily_posts"])
            entry["daily_posts"] = [p for p in entry["daily_posts"] if p.get("date") >= week_ago]
            if len(entry["daily_posts"]) != old_len:
                changed = True
    if changed:
        save_config(config)

# ===== ФУНКЦИИ ДЛЯ РАБОТЫ С VK =====
def vk_api_request(method, params, token):
    url = f"https://api.vk.com/method/{method}"
    params["access_token"] = token
    params["v"] = "5.131"
    try:
        response = requests.get(url, params=params, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if "error" in data:
                logger.error(f"VK API error: {data['error']['error_msg']}")
                return None
            return data.get("response")
        else:
            logger.error(f"HTTP error: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Exception in VK request: {e}")
        return None

def get_last_posts(group_id, token, count=5):
    params = {"owner_id": group_id, "count": count}
    response = vk_api_request("wall.get", params, token)
    if response and "items" in response:
        return response["items"]
    return []

# ===== ФУНКЦИЯ ДЛЯ ФОРМАТИРОВАНИЯ ПОСТА ПОД TELEGRAM =====
def format_post_for_telegram(text, post_link):
    if not text:
        text = "Новый пост без текста"
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        line = line.strip()
        if line:
            cleaned_lines.append(line)
    formatted = '\n\n'.join(cleaned_lines)
    if len(formatted) > MAX_MESSAGE_LENGTH - 100:
        formatted = formatted[:MAX_MESSAGE_LENGTH - 100] + "..."
    result = f"{formatted}\n\n➡️ {post_link}"
    return result

# ===== ФУНКЦИИ ДЛЯ ОТПРАВКИ В TELEGRAM =====
async def send_to_telegram(tg_token, chat_id, text, attachments=None):
    bot = None
    try:
        bot = Bot(token=tg_token)
        if attachments:
            photo_url = None
            for att in attachments:
                if att.get("type") == "photo":
                    sizes = att["photo"].get("sizes", [])
                    if sizes:
                        largest = max(sizes, key=lambda x: x.get("width", 0) * x.get("height", 0))
                        photo_url = largest.get("url")
                        break
            if photo_url:
                try:
                    resp = requests.get(photo_url, timeout=30)
                    if resp.status_code == 200:
                        await bot.send_photo(chat_id=chat_id, photo=resp.content, caption=text[:1024])
                        return True
                except Exception as e:
                    logger.error(f"Error sending photo: {e}")
        if len(text) > MAX_MESSAGE_LENGTH:
            for i in range(0, len(text), MAX_MESSAGE_LENGTH):
                chunk = text[i:i+MAX_MESSAGE_LENGTH]
                await bot.send_message(chat_id=chat_id, text=chunk)
                await asyncio.sleep(2)
        else:
            await bot.send_message(chat_id=chat_id, text=text)
        return True
    except Exception as e:
        logger.error(f"Error sending to Telegram: {e}")
        return False
    finally:
        if bot:
            await bot.close()

# ===== ФОНОВЫЙ ПОТОК =====
async def check_new_posts(context: ContextTypes.DEFAULT_TYPE):
    clear_old_daily_posts()
    config = load_config()
    today = datetime.now().strftime("%Y-%m-%d")

    for user_id_str, entries in config.items():
        user_id = int(user_id_str)
        for entry in entries:
            if not entry["enabled"]:
                continue
            vk_token = entry["vk_token"]
            vk_group_id = entry["vk_group_id"]
            tg_token = entry["tg_token"]
            tg_chat_id = entry["tg_chat_id"]
            last_post_id = entry["last_post_id"]

            # Список уже отправленных сегодня post_id
            already_sent = [p["post_id"] for p in entry["daily_posts"] if p.get("date") == today]
            logger.info(f"🔍 Проверка группы {vk_group_id}: last_post_id={last_post_id}, уже отправлено сегодня: {already_sent}")

            today_count = get_today_post_count(user_id, vk_group_id)
            if today_count >= MAX_POSTS_PER_DAY:
                logger.info(f"⏹️ Лимит постов для группы {vk_group_id} на сегодня ({MAX_POSTS_PER_DAY}) достигнут")
                continue

            posts = get_last_posts(vk_group_id, vk_token, count=5)
            if not posts:
                continue

            # Сортируем по ID (новые – больше)
            posts.sort(key=lambda x: x["id"])
            new_posts = []
            for post in posts:
                if post["id"] > last_post_id and post["id"] not in already_sent:
                    new_posts.append(post)

            if not new_posts:
                logger.info(f"📭 Нет новых постов для группы {vk_group_id}")
                continue

            logger.info(f"📦 Найдено {len(new_posts)} новых постов для группы {vk_group_id}: {[p['id'] for p in new_posts]}")

            for post in new_posts:
                if today_count >= MAX_POSTS_PER_DAY:
                    break

                text = post.get("text", "")
                link = f"https://vk.com/wall{vk_group_id}_{post['id']}"
                formatted_text = format_post_for_telegram(text, link)
                attachments = post.get("attachments", [])
                success = await send_to_telegram(tg_token, tg_chat_id, formatted_text, attachments)

                if success:
                    # Обновляем last_post_id
                    update_last_post_id(user_id, vk_group_id, post["id"])
                    # Отмечаем как отправленный сегодня
                    mark_post_sent(user_id, vk_group_id, post["id"])
                    today_count += 1
                    logger.info(f"✅ Отправлен пост {post['id']} для группы {vk_group_id} (сегодня {today_count})")
                else:
                    logger.error(f"❌ Не удалось отправить пост {post['id']}")

                await asyncio.sleep(SEND_DELAY)

# ===== ОБРАБОТЧИКИ КОМАНД =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для пересылки постов из ВК в Telegram.\n\n"
        "🔧 Команды:\n"
        "/add - добавить связку\n"
        "/list - показать все связки\n"
        "/delete <ID_группы> - удалить связку\n"
        "/toggle <ID_группы> - включить/выключить\n"
        "/help - помощь"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔧 Команды:\n"
        "/add - добавить связку\n"
        "/list - список связок\n"
        "/delete <ID_группы> - удалить\n"
        "/toggle <ID_группы> - включить/выключить\n"
        "/help - помощь"
    )

async def add_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    configs = get_user_configs(user_id)
    if len(configs) >= MAX_CONFIGS_PER_USER:
        await update.message.reply_text(f"❌ Вы уже добавили максимум {MAX_CONFIGS_PER_USER} связок.")
        return
    await update.message.reply_text(
        "📝 Отправьте данные в формате:\n"
        "`VK_TOKEN VK_GROUP_ID TG_TOKEN TG_CHAT_ID`\n\n"
        "Пример:\n"
        "`vk1.a.xxx -12345678 123456:ABCdef @my_channel`",
        parse_mode="Markdown"
    )
    context.user_data["awaiting_add"] = True

async def handle_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_add"):
        return
    user_id = update.effective_user.id
    text = update.message.text.strip()
    parts = text.split()
    if len(parts) != 4:
        await update.message.reply_text("❌ Неверный формат. Нужно 4 параметра.")
        context.user_data["awaiting_add"] = False
        return
    vk_token, vk_group_id_str, tg_token, tg_chat_id = parts
    try:
        vk_group_id = int(vk_group_id_str)
    except ValueError:
        await update.message.reply_text("❌ VK_GROUP_ID должен быть числом (с минусом).")
        context.user_data["awaiting_add"] = False
        return
    configs = get_user_configs(user_id)
    if len(configs) >= MAX_CONFIGS_PER_USER:
        await update.message.reply_text(f"❌ Достигнут лимит {MAX_CONFIGS_PER_USER} связок.")
        context.user_data["awaiting_add"] = False
        return
    if not vk_token.startswith("vk1.a."):
        await update.message.reply_text("❌ VK_TOKEN должен начинаться с 'vk1.a.'")
        context.user_data["awaiting_add"] = False
        return
    for entry in configs:
        if entry["vk_group_id"] == vk_group_id:
            await update.message.reply_text(f"❌ Связка для группы {vk_group_id} уже существует.")
            context.user_data["awaiting_add"] = False
            return
    success = add_user_config(user_id, vk_token, vk_group_id, tg_token, tg_chat_id)
    if success:
        await update.message.reply_text("✅ Связка успешно добавлена!")
    else:
        await update.message.reply_text("❌ Не удалось добавить связку (возможно, дубликат).")
    context.user_data["awaiting_add"] = False

async def list_configs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    configs = get_user_configs(user_id)
    if not configs:
        await update.message.reply_text("📭 Нет связок.")
        return
    lines = []
    for idx, entry in enumerate(configs):
        status = "✅" if entry["enabled"] else "❌"
        vk_group_id = entry["vk_group_id"]
        tg_chat_id = entry["tg_chat_id"]
        last_post_id = entry["last_post_id"]
        lines.append(f"{idx+1}. {status} Группа {vk_group_id} → Канал {tg_chat_id} (last_post: {last_post_id})")
    await update.message.reply_text("📋 Ваши связки:\n" + "\n".join(lines))

async def delete_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("❌ Укажите ID группы. Пример: /delete -197687739")
        return
    try:
        vk_group_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ ID группы должен быть числом.")
        return
    success = delete_user_config(user_id, vk_group_id)
    if success:
        await update.message.reply_text(f"✅ Связка для группы {vk_group_id} удалена.")
    else:
        await update.message.reply_text(f"❌ Связка для группы {vk_group_id} не найдена.")

async def toggle_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("❌ Укажите ID группы. Пример: /toggle -197687739")
        return
    try:
        vk_group_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ ID группы должен быть числом.")
        return
    configs = get_user_configs(user_id)
    target = None
    for entry in configs:
        if entry["vk_group_id"] == vk_group_id:
            target = entry
            break
    if not target:
        await update.message.reply_text(f"❌ Связка для группы {vk_group_id} не найдена.")
        return
    new_enabled = not target["enabled"]
    toggle_enable(user_id, vk_group_id, new_enabled)
    status = "включена" if new_enabled else "выключена"
    await update.message.reply_text(f"✅ Связка для группы {vk_group_id} {status}.")

# ===== ЗАПУСК =====
def main():
    application = Application.builder().token(MAIN_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add", add_config))
    application.add_handler(CommandHandler("list", list_configs))
    application.add_handler(CommandHandler("delete", delete_config))
    application.add_handler(CommandHandler("toggle", toggle_config))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_input))

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(check_new_posts, interval=30, first=10)

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()