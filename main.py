import os
import logging
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

def is_admin(uid):
    return uid == ADMIN_ID or uid == ADMIN2_ID

def get_client_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📞 Связаться с продавцом", callback_data="contact")],
    ])

def get_admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Ответить клиенту", callback_data="reply")],
    ])

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = update.effective_user
    client_name = user.first_name or ""
    if user.last_name:
        client_name += f" {user.last_name}"
    if user.username:
        client_name += f" (@{user.username})"
    
    # Приветствие и сохранение имени в контексте (не в БД)
    ctx.user_data['client_name'] = client_name
    ctx.user_data['client_id'] = uid

    if is_admin(uid):
        text = "🔐 *Админ-панель*\n\nНажми кнопку, чтобы ответить клиенту."
        keyboard = get_admin_keyboard()
    else:
        text = f"👋 *Добро пожаловать, {client_name}!*\n\nНажми на кнопку ниже, чтобы связаться с продавцом."
        keyboard = get_client_keyboard()

    await update.message.reply_text(text, reply_markup=keyboard, parse_mode='Markdown')

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = update.effective_user.id

    if data == "contact":
        await q.edit_message_text(
            "📞 *Напиши своё сообщение сюда.*\n\nПродавец получит его и ответит тебе в этом чате.",
            parse_mode='Markdown'
        )

    elif data == "reply":
        ctx.user_data['awaiting'] = 'reply_text'
        await q.edit_message_text(
            "✏️ *Напиши текст ответа клиенту.*\n\n(Бот отправит его последнему клиенту, который писал вам)",
            parse_mode='Markdown'
        )

    elif data == "back":
        if is_admin(uid):
            text = "🔐 *Админ-панель*\n\nНажми кнопку, чтобы ответить клиенту."
            keyboard = get_admin_keyboard()
        else:
            user = update.effective_user
            client_name = user.first_name or ""
            if user.last_name:
                client_name += f" {user.last_name}"
            if user.username:
                client_name += f" (@{user.username})"
            text = f"👋 *Добро пожаловать, {client_name}!*\n\nНажми на кнопку ниже, чтобы связаться с продавцом."
            keyboard = get_client_keyboard()
        await q.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    user = update.effective_user
    awaiting = ctx.user_data.get('awaiting')
    
    # --- Если клиент пишет админу ---
    if not is_admin(uid):
        client_name = user.first_name or ""
        if user.last_name:
            client_name += f" {user.last_name}"
        if user.username:
            client_name += f" (@{user.username})"

        # Отправляем подтверждение клиенту
        await update.message.reply_text(
            "✅ *Сообщение отправлено!*\n\nПродавец ответит вам в этом чате.",
            reply_markup=get_client_keyboard(),
            parse_mode='Markdown'
        )

        # Сохраняем ID клиента в контексте админа (чтобы знать, кому отвечать)
        ctx.bot_data['last_client_id'] = uid
        ctx.bot_data['last_client_name'] = client_name

        # Отправляем уведомление админам
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Ответить", callback_data="reply")]
        ])
        msg_text = f"💬 *Новое сообщение от {client_name} (ID: {uid}):*\n\n{text}"

        await ctx.bot.send_message(ADMIN_ID, msg_text, reply_markup=kb, parse_mode='Markdown')
        if ADMIN2_ID and ADMIN2_ID != ADMIN_ID:
            await ctx.bot.send_message(ADMIN2_ID, msg_text, reply_markup=kb, parse_mode='Markdown')

        return

    # --- Если админ отвечает клиенту ---
    if awaiting == 'reply_text':
        client_id = ctx.bot_data.get('last_client_id')
        client_name = ctx.bot_data.get('last_client_name', 'Клиент')

        if not client_id:
            await update.message.reply_text(
                "❌ Нет активного диалога с клиентом. Попросите клиента написать вам первым.",
                reply_markup=get_admin_keyboard()
            )
            ctx.user_data.clear()
            return

        try:
            await ctx.bot.send_message(
                client_id,
                f"📩 *Ответ от продавца:*\n\n{text}",
                parse_mode='Markdown'
            )
            await update.message.reply_text(
                f"✅ *Ответ отправлен клиенту {client_name}!*",
                reply_markup=get_admin_keyboard(),
                parse_mode='Markdown'
            )
            ctx.user_data.clear()
        except Exception as e:
            logger.error(f"Ошибка отправки клиенту: {e}")
            await update.message.reply_text(
                "❌ Не удалось отправить сообщение клиенту (возможно, он заблокировал бота).",
                reply_markup=get_admin_keyboard()
            )
            ctx.user_data.clear()

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

    logger.info("🤖 Бот запущен и готов к работе! (Без базы данных)")

    bot = Application.builder().token(TOKEN).build()
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(CallbackQueryHandler(button_handler))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Запускаем Flask в отдельном потоке (для хостинга)
    import threading
    port = int(os.environ.get('PORT', 10000))
    threading.Thread(
        target=lambda: app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False),
        daemon=True
    ).start()

    bot.run_polling()

if __name__ == "__main__":
    main()