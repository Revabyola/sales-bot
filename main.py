import os
import logging
import asyncio
import threading
from flask import Flask
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# --- Настройка ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
ADMIN2_ID = int(os.environ.get("ADMIN2_ID", "0"))

# Хранилище сообщений в памяти
chat_history = {}
active_chats = {}

def is_admin(uid):
    return uid == ADMIN_ID or uid == ADMIN2_ID

def get_client_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📞 Связаться с Мастером", callback_data="contact")],
    ])

def get_admin_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Список чатов", callback_data="chat_list")],
    ])

def get_chat_list_keyboard():
    kb = []
    for client_id, messages in chat_history.items():
        if messages:
            name = messages[0].get('name', f'ID: {client_id}')
            kb.append([InlineKeyboardButton(f"💬 {name}", callback_data=f"open_chat_{client_id}")])
    
    if not kb:
        kb.append([InlineKeyboardButton("📭 Нет сообщений", callback_data="noop")])
    
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    return InlineKeyboardMarkup(kb)

def get_chat_keyboard(client_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 К списку чатов", callback_data="chat_list")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="back")],
    ])

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = update.effective_user
    client_name = user.first_name or ""
    if user.last_name:
        client_name += f" {user.last_name}"
    if user.username:
        client_name += f" (@{user.username})"

    if is_admin(uid):
        text = "🔐 *Панель Мастера*\n\nВыбери действие:"
        keyboard = get_admin_main_keyboard()
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode='Markdown')
    else:
        text = f"👋 *Добро пожаловать, {client_name}!*\n\nНажми на кнопку ниже, чтобы связаться с Мастером."
        keyboard = get_client_keyboard()
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode='Markdown')

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = update.effective_user.id

    if data == "contact":
        await q.edit_message_text(
            "📞 *Напиши своё сообщение сюда.*\n\nМастер получит его и ответит тебе в этом чате.",
            parse_mode='Markdown'
        )

    elif data == "chat_list":
        await show_chat_list(q)

    elif data.startswith("open_chat_"):
        client_id = int(data.split("_")[-1])
        active_chats[uid] = client_id
        await show_chat_messages(q, client_id)

    elif data == "back":
        if is_admin(uid):
            text = "🔐 *Панель Мастера*\n\nВыбери действие:"
            keyboard = get_admin_main_keyboard()
        else:
            user = update.effective_user
            client_name = user.first_name or ""
            if user.last_name:
                client_name += f" {user.last_name}"
            if user.username:
                client_name += f" (@{user.username})"
            text = f"👋 *Добро пожаловать, {client_name}!*\n\nНажми на кнопку ниже, чтобы связаться с Мастером."
            keyboard = get_client_keyboard()
        await q.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')

    elif data == "noop":
        await q.answer("Нет сообщений")

async def show_chat_list(q):
    if not chat_history:
        await q.edit_message_text(
            "💬 Нет активных чатов.",
            reply_markup=get_admin_main_keyboard()
        )
        return

    text = "💬 *Список чатов с клиентами:*\n\n"
    for client_id, messages in chat_history.items():
        if messages:
            name = messages[0].get('name', f'ID: {client_id}')
            last_msg = messages[-1].get('text', '')[:50]
            text += f"👤 *{name}*\n📝 {last_msg}...\n\n"

    await q.edit_message_text(
        text,
        reply_markup=get_chat_list_keyboard(),
        parse_mode='Markdown'
    )

async def show_chat_messages(q, client_id):
    messages = chat_history.get(client_id, [])
    name = messages[0].get('name', f'ID: {client_id}') if messages else f'ID: {client_id}'

    if not messages:
        await q.edit_message_text(
            f"💬 *Чат с {name}*\n\nНет сообщений.",
            reply_markup=get_chat_keyboard(client_id),
            parse_mode='Markdown'
        )
        return

    text = f"💬 *Чат с {name}*\n\n"
    for msg in messages:
        prefix = "🤵 *Мастер*" if msg.get('from_admin') else f"👤 *{name}*"
        text += f"{prefix}:\n{msg['text']}\n\n"

    await q.edit_message_text(
        text,
        reply_markup=get_chat_keyboard(client_id),
        parse_mode='Markdown'
    )

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    user = update.effective_user

    if not is_admin(uid):
        client_name = user.first_name or ""
        if user.last_name:
            client_name += f" {user.last_name}"
        if user.username:
            client_name += f" (@{user.username})"

        if uid not in chat_history:
            chat_history[uid] = []
        chat_history[uid].append({
            'text': text,
            'from_admin': False,
            'name': client_name
        })

        await update.message.reply_text(
            "✅ *Сообщение отправлено!*\n\nМастер ответит вам в этом чате.",
            reply_markup=get_client_keyboard(),
            parse_mode='Markdown'
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Ответить", callback_data=f"open_chat_{uid}")]
        ])
        msg_text = f"💬 *Новое сообщение от {client_name}:*\n\n{text}"

        await ctx.bot.send_message(ADMIN_ID, msg_text, reply_markup=kb, parse_mode='Markdown')
        if ADMIN2_ID and ADMIN2_ID != ADMIN_ID:
            await ctx.bot.send_message(ADMIN2_ID, msg_text, reply_markup=kb, parse_mode='Markdown')

        return

    client_id = active_chats.get(uid)

    if not client_id:
        if chat_history:
            client_id = next(iter(chat_history.keys()))
            active_chats[uid] = client_id
        else:
            await update.message.reply_text(
                "❌ Нет активных чатов. Нажми 'Список чатов' и выбери клиента.",
                reply_markup=get_admin_main_keyboard()
            )
            return

    if client_id not in chat_history:
        await update.message.reply_text(
            "❌ Этот чат уже закрыт.",
            reply_markup=get_admin_main_keyboard()
        )
        active_chats.pop(uid, None)
        return

    client_name = chat_history[client_id][0].get('name', f'ID: {client_id}')

    chat_history[client_id].append({
        'text': text,
        'from_admin': True,
        'name': 'Мастер'
    })

    try:
        await ctx.bot.send_message(
            client_id,
            f"📩 *Ответ от Мастера:*\n\n{text}",
            parse_mode='Markdown'
        )
        await update.message.reply_text(
            f"✅ *Ответ отправлен клиенту {client_name}!*",
            reply_markup=get_admin_main_keyboard(),
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Ошибка отправки клиенту: {e}")
        await update.message.reply_text(
            "❌ Не удалось отправить сообщение клиенту.",
            reply_markup=get_admin_main_keyboard()
        )

@app.route('/')
def home():
    return "Бот работает!", 200

@app.route('/health')
def health():
    return "OK", 200

def run_bot():
    """Запускает бота с проверкой подключения"""
    if not TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN не установлен!")
        return
    
    # Создаём цикл событий для этого потока
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Проверяем подключение к Telegram
    logger.info("🔍 Проверяем подключение к Telegram API...")
    
    bot = Application.builder().token(TOKEN).build()
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(CallbackQueryHandler(button_handler))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    # Пробуем получить информацию о боте
    try:
        import asyncio
        async def test_connection():
            try:
                me = await bot.bot.get_me()
                logger.info(f"✅ Подключение к Telegram успешно! Бот: @{me.username}")
                return True
            except Exception as e:
                logger.error(f"❌ Ошибка подключения к Telegram: {e}")
                return False
        
        # Запускаем проверку
        result = loop.run_until_complete(test_connection())
        if not result:
            logger.error("❌ Не удалось подключиться к Telegram API. Проверьте токен и интернет.")
            return
            
    except Exception as e:
        logger.error(f"❌ Ошибка при проверке подключения: {e}")
        return
    
    logger.info("🤖 Бот запущен и готов к работе!")
    bot.run_polling()

if __name__ == "__main__":
    if not TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN не установлен!")
        exit(1)
    
    # Запускаем бота в отдельном потоке
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Запускаем Flask для Render
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"🚀 Flask запущен на порту {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)