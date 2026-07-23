import asyncio
import logging
import os
import sys
import sqlite3
import time
import json
import requests
import re
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== ЗАГРУЗКА ТОКЕНА ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ =====
# Используем переменную BOT_TOKEN, которую вы добавили на Ботхосте
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не задан в переменных окружения! Бот не запустится.")
    sys.exit(1)
logger.info("✅ Токен загружен из переменной окружения")

# Путь к базе данных (хранилище настроек)
DB_PATH = os.path.join("/data", "vk2tg.db")
# Если используется локально, можно заменить на путь в текущей директории

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        vk_token TEXT,
        vk_group_id INTEGER,
        tg_token TEXT,
        tg_chat_id TEXT,
        last_post_id INTEGER DEFAULT 0,
        enabled INTEGER DEFAULT 1
    )''')
    conn.commit()
    conn.close()

init_db()

# Функции работы с БД
def get_user_configs(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT vk_token, vk_group_id, tg_token, tg_chat_id, last_post_id, enabled FROM users WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def add_user_config(user_id, vk_token, vk_group_id, tg_token, tg_chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO users (user_id, vk_token, vk_group_id, tg_token, tg_chat_id, last_post_id, enabled) VALUES (?, ?, ?, ?, ?, 0, 1)",
              (user_id, vk_token, vk_group_id, tg_token, tg_chat_id))
    conn.commit()
    conn.close()

def delete_user_config(user_id, index):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # удаляем по индексу (нумерация с 0)
    rows = get_user_configs(user_id)
    if index < len(rows):
        # удаляем первую запись, если index=0, и т.д. - проще удалить все и пересоздать, но для простоты удалим по id
        # Здесь упрощенно: удаляем все, потом заново добавим, но мы реализуем удаление по номеру
        # Более правильно: удалить конкретную запись по rowid
        c.execute("DELETE FROM users WHERE rowid = (SELECT rowid FROM users WHERE user_id = ? LIMIT 1 OFFSET ?)", (user_id, index))
        conn.commit()
    conn.close()

def update_last_post_id(user_id, vk_group_id, last_post_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET last_post_id = ? WHERE user_id = ? AND vk_group_id = ?", (last_post_id, user_id, vk_group_id))
    conn.commit()
    conn.close()

def toggle_enable(user_id, index, enabled):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # получаем rowid нужной записи
    c.execute("SELECT rowid FROM users WHERE user_id = ? LIMIT 1 OFFSET ?", (user_id, index))
    row = c.fetchone()
    if row:
        c.execute("UPDATE users SET enabled = ? WHERE rowid = ?", (enabled, row[0]))
        conn.commit()
    conn.close()

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

# ===== ФУНКЦИИ ДЛЯ ОТПРАВКИ В TELEGRAM =====
async def send_to_telegram(tg_token, chat_id, text, attachments=None):
    """Отправляет пост в Telegram-канал через бота-исполнителя."""
    bot = None
    try:
        from telegram import Bot
        bot = Bot(token=tg_token)
        # Если есть вложения (фото), отправляем с фото
        if attachments:
            # Пока упрощенно: берем первое фото
            photo_url = None
            for att in attachments:
                if att.get("type") == "photo":
                    sizes = att["photo"].get("sizes", [])
                    if sizes:
                        # берем самую большую
                        largest = max(sizes, key=lambda x: x.get("width", 0) * x.get("height", 0))
                        photo_url = largest.get("url")
                        break
            if photo_url:
                # Скачиваем фото и отправляем
                try:
                    resp = requests.get(photo_url, timeout=30)
                    if resp.status_code == 200:
                        await bot.send_photo(chat_id=chat_id, photo=resp.content, caption=text[:1024])
                        return True
                except Exception as e:
                    logger.error(f"Error sending photo: {e}")
        # Если не удалось отправить с фото, отправляем текст
        await bot.send_message(chat_id=chat_id, text=text[:4096])
        return True
    except Exception as e:
        logger.error(f"Error sending to Telegram: {e}")
        return False
    finally:
        if bot:
            await bot.close()

# ===== ФОНОВЫЙ ПОТОК ДЛЯ ПРОВЕРКИ НОВЫХ ПОСТОВ =====
async def check_new_posts(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет все активные конфигурации на наличие новых постов."""
    # Получаем все активные конфигурации (enabled=1)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, vk_token, vk_group_id, tg_token, tg_chat_id, last_post_id FROM users WHERE enabled = 1")
    configs = c.fetchall()
    conn.close()

    for user_id, vk_token, vk_group_id, tg_token, tg_chat_id, last_post_id in configs:
        try:
            # Получаем последние 5 постов
            posts = get_last_posts(vk_group_id, vk_token, count=5)
            if not posts:
                continue
            # Ищем новые посты (с ID больше last_post_id)
            new_posts = []
            for post in posts:
                if post["id"] > last_post_id:
                    new_posts.append(post)
            # Сортируем по возрастанию ID (чтобы сначала старые)
            new_posts.sort(key=lambda x: x["id"])
            for post in new_posts:
                # Формируем текст
                text = post.get("text", "")
                if not text:
                    text = "Новый пост без текста"
                # Добавляем ссылку на пост
                link = f"https://vk.com/wall{vk_group_id}_{post['id']}"
                full_text = f"{text}\n\n➡️ {link}"
                # Проверяем вложения
                attachments = post.get("attachments", [])
                # Отправляем в Telegram
                success = await send_to_telegram(tg_token, tg_chat_id, full_text, attachments)
                if success:
                    # Обновляем last_post_id
                    update_last_post_id(user_id, vk_group_id, post["id"])
                else:
                    logger.error(f"Failed to send post {post['id']} for user {user_id}")
                # Небольшая задержка между постами
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error processing config for user {user_id}: {e}")

# ===== ОБРАБОТЧИКИ КОМАНД TELEGRAM =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(
        "👋 Привет! Я бот для пересылки постов из ВК в Telegram.\n\n"
        "🔧 Команды:\n"
        "/add - добавить новую связку (группа ВК → канал)\n"
        "/list - показать все ваши связки\n"
        "/delete <номер> - удалить связку по номеру\n"
        "/toggle <номер> - включить/выключить связку\n"
        "/help - показать это сообщение"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔧 Команды:\n"
        "/add - добавить новую связку (группа ВК → канал)\n"
        "/list - показать все ваши связки\n"
        "/delete <номер> - удалить связку по номеру\n"
        "/toggle <номер> - включить/выключить связку\n"
        "/help - показать это сообщение"
    )

async def add_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # Проверяем, не достигнут ли лимит (3 конфигурации)
    configs = get_user_configs(user_id)
    if len(configs) >= 3:
        await update.message.reply_text("❌ Вы уже добавили максимум 3 связки.")
        return
    await update.message.reply_text(
        "📝 Для добавления новой связки отправьте мне данные в формате:\n"
        "`VK_TOKEN VK_GROUP_ID TG_TOKEN TG_CHAT_ID`\n\n"
        "Где:\n"
        "• VK_TOKEN — сервисный ключ вашей группы ВК\n"
        "• VK_GROUP_ID — ID группы ВК (с минусом, например -12345678)\n"
        "• TG_TOKEN — токен Telegram-бота для канала (от @BotFather)\n"
        "• TG_CHAT_ID — ID канала (например, @channel или -1001234567890)\n\n"
        "Пример:\n"
        "`vk1.a.xxx -12345678 123456:ABCdef @my_channel`",
        parse_mode="Markdown"
    )
    # Переключаем состояние ожидания ввода
    context.user_data["awaiting_add"] = True

async def handle_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_add"):
        return
    user_id = update.effective_user.id
    text = update.message.text.strip()
    parts = text.split()
    if len(parts) != 4:
        await update.message.reply_text("❌ Неверный формат. Нужно 4 параметра.")
        return
    vk_token, vk_group_id_str, tg_token, tg_chat_id = parts
    try:
        vk_group_id = int(vk_group_id_str)
    except ValueError:
        await update.message.reply_text("❌ VK_GROUP_ID должен быть числом (с минусом).")
        return
    # Проверяем, что лимит не превышен
    configs = get_user_configs(user_id)
    if len(configs) >= 3:
        await update.message.reply_text("❌ Вы уже добавили максимум 3 связки.")
        return
    # Проверяем токены (простые проверки)
    if not vk_token.startswith("vk1.a."):
        await update.message.reply_text("❌ Похоже, VK_TOKEN неверный. Он должен начинаться с 'vk1.a.'")
        return
    if not tg_token or not tg_chat_id:
        await update.message.reply_text("❌ TG_TOKEN или TG_CHAT_ID не могут быть пустыми.")
        return
    # Добавляем конфигурацию
    add_user_config(user_id, vk_token, vk_group_id, tg_token, tg_chat_id)
    await update.message.reply_text("✅ Связка успешно добавлена! Бот начнёт отслеживать посты.")
    context.user_data["awaiting_add"] = False

async def list_configs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    configs = get_user_configs(user_id)
    if not configs:
        await update.message.reply_text("📭 У вас нет добавленных связок.")
        return
    lines = []
    for idx, (vk_token, vk_group_id, tg_token, tg_chat_id, last_post_id, enabled) in enumerate(configs):
        status = "✅" if enabled else "❌"
        lines.append(f"{idx+1}. {status} Группа {vk_group_id} → Канал {tg_chat_id} (last_post: {last_post_id})")
    await update.message.reply_text("📋 Ваши связки:\n" + "\n".join(lines))

async def delete_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("❌ Укажите номер связки для удаления. Пример: /delete 1")
        return
    try:
        idx = int(args[0]) - 1
    except ValueError:
        await update.message.reply_text("❌ Номер должен быть числом.")
        return
    configs = get_user_configs(user_id)
    if idx < 0 or idx >= len(configs):
        await update.message.reply_text("❌ Связки с таким номером нет.")
        return
    delete_user_config(user_id, idx)
    await update.message.reply_text("✅ Связка удалена.")

async def toggle_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("❌ Укажите номер связки для включения/выключения. Пример: /toggle 1")
        return
    try:
        idx = int(args[0]) - 1
    except ValueError:
        await update.message.reply_text("❌ Номер должен быть числом.")
        return
    configs = get_user_configs(user_id)
    if idx < 0 or idx >= len(configs):
        await update.message.reply_text("❌ Связки с таким номером нет.")
        return
    current_enabled = configs[idx][5]  # enabled
    new_enabled = 0 if current_enabled else 1
    toggle_enable(user_id, idx, new_enabled)
    status = "включена" if new_enabled else "выключена"
    await update.message.reply_text(f"✅ Связка {idx+1} {status}.")

# ===== ЗАПУСК БОТА =====
def main():
    # Создаем приложение
    application = Application.builder().token(BOT_TOKEN).build()

    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add", add_config))
    application.add_handler(CommandHandler("list", list_configs))
    application.add_handler(CommandHandler("delete", delete_config))
    application.add_handler(CommandHandler("toggle", toggle_config))
    # Обработчик для ввода данных после /add
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_input))

    # Добавляем фоновую задачу (проверка каждые 30 секунд)
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(check_new_posts, interval=30, first=10)

    # Запускаем бота
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()