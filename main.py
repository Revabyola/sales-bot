import os
import logging
from datetime import datetime, timedelta
from flask import Flask
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import psycopg2

# --- Настройка ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DATABASE_URL = os.environ.get('DATABASE_URL')
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
ADMIN2_ID = int(os.environ.get("ADMIN2_ID", "0"))

def is_admin(uid):
    return uid == ADMIN_ID or uid == ADMIN2_ID

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id SERIAL PRIMARY KEY,
            client_id BIGINT,
            client_name TEXT,
            message TEXT,
            from_admin BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    logger.info("База данных готова")

def get_client_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📞 Связаться с продавцом", callback_data="contact")],
    ])

def get_admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Сообщения от клиентов", callback_data="messages")],
    ])

def local_time(dt):
    if dt:
        return (dt + timedelta(hours=3)).strftime('%d.%m.%Y %H:%M')
    return ''

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = update.effective_user
    client_name = user.first_name or ""
    if user.last_name:
        client_name += f" {user.last_name}"
    if user.username:
        client_name += f" (@{user.username})"
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO chat_messages (client_id, client_name, message, from_admin)
        VALUES (%s, %s, %s, %s)
    """, (uid, client_name, "👋 Начал диалог с ботом", False))
    conn.commit()
    cur.close()
    conn.close()
    
    if is_admin(uid):
        text = "🔐 *Админ-панель*\n\nВыбери действие:"
        keyboard = get_admin_keyboard()
    else:
        text = "👋 *Добро пожаловать!*\n\nНажми на кнопку ниже, чтобы связаться с продавцом."
        keyboard = get_client_keyboard()
    
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode='Markdown')

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = update.effective_user.id
    
    if data == "contact":
        await q.edit_message_text(
            "📞 *Напиши своё сообщение сюда.*\n\n"
            "Продавец получит его и ответит тебе в этом чате.",
            parse_mode='Markdown'
        )
    
    elif data == "messages":
        await show_chat_list(q)
    
    elif data.startswith("chat_"):
        await show_chat_messages(q, int(data.split("_")[-1]))
    
    elif data.startswith("reply_"):
        ctx.user_data['reply_client_id'] = int(data.split("_")[-1])
        ctx.user_data['awaiting'] = 'reply_text'
        await q.edit_message_text(
            "✏️ *Введи текст ответа клиенту:*",
            parse_mode='Markdown'
        )
    
    elif data == "back":
        if is_admin(uid):
            text = "🔐 *Админ-панель*\n\nВыбери действие:"
            keyboard = get_admin_keyboard()
        else:
            text = "👋 *Добро пожаловать!*\n\nНажми на кнопку ниже, чтобы связаться с продавцом."
            keyboard = get_client_keyboard()
        await q.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')

async def show_chat_list(q):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ON (client_id) 
            client_id, 
            client_name, 
            message, 
            created_at 
        FROM chat_messages 
        WHERE from_admin = FALSE 
        ORDER BY client_id, created_at DESC
    """)
    chats = cur.fetchall()
    cur.close()
    conn.close()
    
    if not chats:
        await q.edit_message_text(
            "💬 Нет сообщений от клиентов.",
            reply_markup=get_admin_keyboard()
        )
        return
    
    text = "💬 *Чаты с клиентами:*\n\n"
    kb = []
    for chat in chats:
        cid, name, msg, dt = chat
        display_name = name or f"ID: {cid}"
        dt_str = local_time(dt)
        text += f"👤 *{display_name}*\n📅 {dt_str}\n\n"
        kb.append([InlineKeyboardButton(f"💬 {display_name}", callback_data=f"chat_{cid}")])
    
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def show_chat_messages(q, client_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT client_name, message, from_admin, created_at 
        FROM chat_messages 
        WHERE client_id = %s 
        ORDER BY created_at ASC
    """, (client_id,))
    msgs = cur.fetchall()
    cur.close()
    conn.close()
    
    if not msgs:
        await q.edit_message_text(
            "💬 Нет сообщений.",
            reply_markup=get_admin_keyboard()
        )
        return
    
    client_name = msgs[0][0] or f"ID: {client_id}"
    text = f"💬 *Чат с {client_name}:*\n\n"
    
    for name, msg, from_admin, dt in msgs:
        prefix = "🤵 *Вы*" if from_admin else f"👤 *{client_name}*"
        dt_str = local_time(dt)
        text += f"{prefix} [{dt_str}]:\n{msg}\n\n"
    
    kb = [
        [InlineKeyboardButton("✏️ Ответить", callback_data=f"reply_{client_id}")],
        [InlineKeyboardButton("🔙 К списку чатов", callback_data="messages")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    user = update.effective_user
    awaiting = ctx.user_data.get('awaiting')
    
    if not is_admin(uid):
        client_name = user.first_name or ""
        if user.last_name:
            client_name += f" {user.last_name}"
        if user.username:
            client_name += f" (@{user.username})"
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO chat_messages (client_id, client_name, message, from_admin)
            VALUES (%s, %s, %s, %s)
        """, (uid, client_name, text, False))
        conn.commit()
        cur.close()
        conn.close()
        
        await update.message.reply_text(
            "✅ *Сообщение отправлено!*\n\nПродавец ответит вам в этом чате.",
            reply_markup=get_client_keyboard(),
            parse_mode='Markdown'
        )
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Ответить", callback_data=f"reply_{uid}")]
        ])
        msg_text = f"💬 *Новое сообщение от {client_name}:*\n\n{text}"
        
        await ctx.bot.send_message(ADMIN_ID, msg_text, reply_markup=kb, parse_mode='Markdown')
        if ADMIN2_ID and ADMIN2_ID != ADMIN_ID:
            await ctx.bot.send_message(ADMIN2_ID, msg_text, reply_markup=kb, parse_mode='Markdown')
        
        return
    
    if awaiting == 'reply_text':
        client_id = ctx.user_data.get('reply_client_id')
        if not client_id:
            await update.message.reply_text("❌ Ошибка: не выбран клиент для ответа.")
            return
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO chat_messages (client_id, client_name, message, from_admin)
            VALUES (%s, 'Админ', %s, TRUE)
        """, (client_id, text))
        conn.commit()
        cur.close()
        conn.close()
        
        ctx.user_data.clear()
        
        try:
            await ctx.bot.send_message(
                client_id,
                f"📩 *Ответ от продавца:*\n\n{text}",
                parse_mode='Markdown'
            )
            await update.message.reply_text(
                "✅ *Ответ отправлен клиенту!*",
                reply_markup=get_admin_keyboard(),
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Ошибка отправки клиенту: {e}")
            await update.message.reply_text(
                "❌ Не удалось отправить сообщение клиенту (возможно, он заблокировал бота).",
                reply_markup=get_admin_keyboard()
            )
    
    else:
        await update.message.reply_text(
            "❌ Неизвестная команда. Используй кнопки меню.",
            reply_markup=get_admin_keyboard()
        )

@app.route('/health')
def health():
    return "OK", 200

def main():
    if not TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN не установлен!")
        return
    
    if not DATABASE_URL:
        logger.error("❌ DATABASE_URL не установлен!")
        return
    
    init_db()
    
    bot = Application.builder().token(TOKEN).build()
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(CallbackQueryHandler(button_handler))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    logger.info("🤖 Бот запущен и готов к работе!")
    
    import threading
    port = int(os.environ.get('PORT', 10000))
    threading.Thread(
        target=lambda: app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False),
        daemon=True
    ).start()
    
    bot.run_polling()

if __name__ == "__main__":
    main()