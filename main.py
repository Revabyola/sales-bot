import os
import logging
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import psycopg2
from psycopg2.extras import RealDictCursor

# --- Настройка ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DATABASE_URL = os.environ.get('DATABASE_URL')
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            price REAL NOT NULL,
            stock INTEGER NOT NULL DEFAULT 0,
            sold INTEGER NOT NULL DEFAULT 0,
            active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id SERIAL PRIMARY KEY,
            client_id BIGINT NOT NULL,
            message TEXT NOT NULL,
            from_admin BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    conn.commit()
    cur.close()
    conn.close()
    logger.info("База данных готова")

# --- Клавиатуры ---
def get_client_keyboard():
    keyboard = [
        [InlineKeyboardButton("🛍 Каталог товаров", callback_data="catalog")],
        [InlineKeyboardButton("📞 Связаться с продавцом", callback_data="contact")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_keyboard():
    keyboard = [
        [InlineKeyboardButton("➕ Добавить товар", callback_data="add_product")],
        [InlineKeyboardButton("📋 Мои товары", callback_data="my_products")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("💬 Сообщения от клиентов", callback_data="messages")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_catalog_keyboard(products, page=0):
    keyboard = []
    per_page = 5
    start = page * per_page
    end = start + per_page
    current = products[start:end]
    
    for p in current:
        keyboard.append([
            InlineKeyboardButton(
                f"{p['name']} - {p['price']}₽ ({p['stock']} шт.)", 
                callback_data=f"product_{p['id']}"
            )
        ])
    
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Назад", callback_data=f"catalog_page_{page-1}"))
    if end < len(products):
        nav.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"catalog_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    
    keyboard.append([InlineKeyboardButton("🔙 Главное меню", callback_data="back")])
    return InlineKeyboardMarkup(keyboard)

# --- Команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id == ADMIN_ID:
        keyboard = get_admin_keyboard()
        text = "🔐 *Админ-панель*\n\nВыбери действие:"
    else:
        keyboard = get_client_keyboard()
        text = "👋 *Добро пожаловать!*\n\nЯ бот для заказа товаров.\nВыбери действие:"
    
    if update.message:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode='Markdown')
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')

# --- Ответ клиенту ---
async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        return
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ Используй формат:\n`/reply ID текст`\n\n"
            "Например: `/reply 123456789 Ваш заказ готов!`",
            parse_mode='Markdown'
        )
        return
    
    try:
        client_id = int(context.args[0])
        reply_text = ' '.join(context.args[1:])
        
        # Сохраняем в БД
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_messages (client_id, message, from_admin) VALUES (%s, %s, TRUE)",
            (client_id, reply_text)
        )
        conn.commit()
        cur.close()
        conn.close()
        
        # Отправляем клиенту
        await context.bot.send_message(
            client_id,
            f"📩 *Ответ от продавца:*\n\n{reply_text}",
            parse_mode='Markdown'
        )
        
        await update.message.reply_text(f"✅ Ответ отправлен клиенту {client_id}!")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

# --- Обработчик кнопок ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    
    # --- КЛИЕНТ И АДМИН: Каталог ---
    if data == "catalog":
        await show_catalog(query, page=0)
    
    elif data.startswith("catalog_page_"):
        page = int(data.replace("catalog_page_", ""))
        await show_catalog(query, page=page)
    
    elif data.startswith("product_"):
        product_id = int(data.replace("product_", ""))
        await show_product(query, product_id)
    
    elif data == "contact":
        await query.edit_message_text(
            "📞 *Связь с продавцом*\n\n"
            "Напиши своё сообщение прямо сюда, и я передам его продавцу.\n"
            "Он ответит тебе в этом же чате.\n\n"
            "_Просто напиши текст и отправь его._",
            parse_mode='Markdown'
        )
    
    elif data == "back":
        await start(update, context)
    
    # --- АДМИН ---
    elif data == "add_product":
        context.user_data['awaiting'] = 'product_name'
        await query.edit_message_text("✏️ Введи *название товара*:", parse_mode='Markdown')
    
    elif data == "my_products":
        await show_admin_products(query, page=0)
    
    elif data.startswith("admin_products_page_"):
        page = int(data.replace("admin_products_page_", ""))
        await show_admin_products(query, page=page)
    
    elif data.startswith("edit_product_"):
        product_id = int(data.replace("edit_product_", ""))
        await show_edit_menu(query, product_id)
    
    elif data.startswith("delete_product_"):
        product_id = int(data.replace("delete_product_", ""))
        await delete_product(query, product_id)
    
    elif data.startswith("restock_"):
        product_id = int(data.replace("restock_", ""))
        context.user_data['restock_product_id'] = product_id
        context.user_data['awaiting'] = 'restock_amount'
        await query.edit_message_text("📦 Введи количество для пополнения:", parse_mode='Markdown')
    
    elif data.startswith("sell_"):
        product_id = int(data.replace("sell_", ""))
        await sell_product(query, product_id)
    
    elif data == "stats":
        await show_stats(query)
    
    elif data == "messages":
        await show_messages(query)

# --- Продажа товара ---
async def sell_product(query, product_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products WHERE id = %s", (product_id,))
    product = cur.fetchone()
    
    if not product:
        await query.answer("Товар не найден")
        cur.close()
        conn.close()
        return
    
    if product['stock'] <= 0:
        await query.answer("Нет в наличии!")
        cur.close()
        conn.close()
        return
    
    # Списываем 1 штуку
    cur.execute("UPDATE products SET stock = stock - 1, sold = sold + 1 WHERE id = %s", (product_id,))
    conn.commit()
    cur.close()
    conn.close()
    
    await query.answer(f"✅ Продано! Осталось: {product['stock'] - 1} шт.")

async def show_catalog(query, page=0):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products WHERE active = TRUE AND stock > 0 ORDER BY name")
    products = cur.fetchall()
    cur.close()
    conn.close()
    
    if not products:
        await query.edit_message_text(
            "📭 *Товаров пока нет.*\nЗагляни позже!",
            reply_markup=get_client_keyboard(),
            parse_mode='Markdown'
        )
        return
    
    await query.edit_message_text(
        "🛍 *Каталог товаров:*",
        reply_markup=get_catalog_keyboard(products, page),
        parse_mode='Markdown'
    )

async def show_product(query, product_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products WHERE id = %s", (product_id,))
    product = cur.fetchone()
    cur.close()
    conn.close()
    
    if not product:
        await query.answer("Товар не найден")
        return
    
    keyboard = [
        [InlineKeyboardButton("📞 Связаться с продавцом", callback_data="contact")],
        [InlineKeyboardButton("🔙 Назад", callback_data="catalog")],
    ]
    
    desc = f"\n📝 _{product['description']}_" if product['description'] else ""
    await query.edit_message_text(
        f"📦 *{product['name']}*\n\n"
        f"💰 Цена: *{product['price']} ₽*\n"
        f"📦 В наличии: *{product['stock']} шт.*{desc}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

# --- АДМИН: Управление товарами ---
async def show_admin_products(query, page=0):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products ORDER BY name")
    products = cur.fetchall()
    cur.close()
    conn.close()
    
    if not products:
        await query.edit_message_text("📭 Нет товаров.", reply_markup=get_admin_keyboard())
        return
    
    per_page = 5
    start = page * per_page
    end = start + per_page
    
    keyboard = []
    for p in products[start:end]:
        status = "✅" if p['active'] else "❌"
        keyboard.append([
            InlineKeyboardButton(
                f"{status} {p['name']} - {p['price']}₽ ({p['stock']} шт.)",
                callback_data=f"edit_product_{p['id']}"
            )
        ])
    
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"admin_products_page_{page-1}"))
    if end < len(products):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"admin_products_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    
    await query.edit_message_text(
        "📋 *Твои товары:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def show_edit_menu(query, product_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products WHERE id = %s", (product_id,))
    p = cur.fetchone()
    cur.close()
    conn.close()
    
    keyboard = [
        [InlineKeyboardButton("🛒 Продать (-1)", callback_data=f"sell_{product_id}")],
        [InlineKeyboardButton("📦 Пополнить (+)", callback_data=f"restock_{product_id}")],
        [InlineKeyboardButton("❌ Удалить", callback_data=f"delete_product_{product_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data="my_products")],
    ]
    
    await query.edit_message_text(
        f"📦 *{p['name']}*\n"
        f"💰 Цена: {p['price']} ₽\n"
        f"📦 Остаток: {p['stock']} шт.\n"
        f"📊 Продано: {p['sold']} шт.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def delete_product(query, product_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM products WHERE id = %s", (product_id,))
    conn.commit()
    cur.close()
    conn.close()
    
    await query.answer("✅ Товар удалён!")
    await show_admin_products(query)

async def show_stats(query):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("SELECT COUNT(*) as total FROM products WHERE active = TRUE")
    total_products = cur.fetchone()['total']
    
    cur.execute("SELECT COALESCE(SUM(stock), 0) as total_stock FROM products WHERE active = TRUE")
    total_stock = cur.fetchone()['total_stock']
    
    cur.execute("SELECT COALESCE(SUM(sold), 0) as total_sold FROM products")
    total_sold = cur.fetchone()['total_sold']
    
    cur.execute("SELECT COALESCE(SUM(sold * price), 0) as total_revenue FROM products")
    total_revenue = cur.fetchone()['total_revenue']
    
    cur.close()
    conn.close()
    
    await query.edit_message_text(
        "📊 *Статистика:*\n\n"
        f"📦 Всего товаров: *{total_products}*\n"
        f"📋 Остаток: *{total_stock} шт.*\n"
        f"🛒 Продано: *{total_sold} шт.*\n"
        f"💰 Выручка: *{total_revenue} ₽*",
        reply_markup=get_admin_keyboard(),
        parse_mode='Markdown'
    )

async def show_messages(query):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM chat_messages ORDER BY created_at DESC LIMIT 10")
    messages = cur.fetchall()
    cur.close()
    conn.close()
    
    if not messages:
        await query.edit_message_text("💬 Нет сообщений.", reply_markup=get_admin_keyboard())
        return
    
    text = "💬 *Последние сообщения:*\n\n"
    for msg in messages:
        prefix = "🤵 Ты → Клиент" if msg['from_admin'] else "👤 Клиент"
        text += f"{prefix} (ID {msg['client_id']}): {msg['message'][:50]}\n"
    
    text += "\n_Для ответа: `/reply ID текст`_"
    
    await query.edit_message_text(text, reply_markup=get_admin_keyboard(), parse_mode='Markdown')

# --- Обработчик текста ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    awaiting = context.user_data.get('awaiting')
    
    # Админ: добавление товара
    if user_id == ADMIN_ID and awaiting == 'product_name':
        context.user_data['product_name'] = text
        context.user_data['awaiting'] = 'product_price'
        await update.message.reply_text("💰 Введи *цену* (в рублях):", parse_mode='Markdown')
    
    elif user_id == ADMIN_ID and awaiting == 'product_price':
        try:
            price = float(text)
            context.user_data['product_price'] = price
            context.user_data['awaiting'] = 'product_stock'
            await update.message.reply_text("📦 Введи *количество*:", parse_mode='Markdown')
        except:
            await update.message.reply_text("❌ Введи число!")
    
    elif user_id == ADMIN_ID and awaiting == 'product_stock':
        try:
            stock = int(text)
            name = context.user_data['product_name']
            price = context.user_data['product_price']
            
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO products (name, price, stock) VALUES (%s, %s, %s)",
                (name, price, stock)
            )
            conn.commit()
            cur.close()
            conn.close()
            
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ Товар *{name}* добавлен!\n💰 {price} ₽ | 📦 {stock} шт.",
                reply_markup=get_admin_keyboard(),
                parse_mode='Markdown'
            )
        except:
            await update.message.reply_text("❌ Введи число!")
    
    elif user_id == ADMIN_ID and awaiting == 'restock_amount':
        try:
            amount = int(text)
            product_id = context.user_data['restock_product_id']
            
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE products SET stock = stock + %s WHERE id = %s", (amount, product_id))
            conn.commit()
            cur.close()
            conn.close()
            
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ Запас пополнен на *{amount} шт.*!",
                reply_markup=get_admin_keyboard(),
                parse_mode='Markdown'
            )
        except:
            await update.message.reply_text("❌ Введи число!")
    
    # Клиент: сообщение продавцу
    elif user_id != ADMIN_ID:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_messages (client_id, message) VALUES (%s, %s)",
            (user_id, text)
        )
        conn.commit()
        cur.close()
        conn.close()
        
        await context.bot.send_message(
            ADMIN_ID,
            f"💬 *Сообщение от клиента* (ID: {user_id}):\n\n{text}\n\n_Ответить: `/reply {user_id} текст`_",
            parse_mode='Markdown'
        )
        
        await update.message.reply_text(
            "✅ *Сообщение отправлено!*\nПродавец скоро свяжется с тобой.",
            reply_markup=get_client_keyboard(),
            parse_mode='Markdown'
        )

@app.route('/health')
def health():
    return "OK", 200

def main():
    if not TOKEN:
        logger.error("Токен не найден!")
        return
    
    init_db()
    
    app_telegram = Application.builder().token(TOKEN).build()
    app_telegram.add_handler(CommandHandler("start", start))
    app_telegram.add_handler(CommandHandler("reply", reply_command))
    app_telegram.add_handler(CallbackQueryHandler(button_handler))
    app_telegram.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    logger.info("Бот для продаж запущен!")
    
    import threading
    def run_flask():
        port = int(os.environ.get('PORT', 10000))
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    threading.Thread(target=run_flask, daemon=True).start()
    
    app_telegram.run_polling()

if __name__ == "__main__":
    main()