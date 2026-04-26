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
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS debts (
            id SERIAL PRIMARY KEY,
            product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
            debtor_name TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0,
            returned BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
        [InlineKeyboardButton("📝 Долги", callback_data="debts_list")],
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
                f"{p['name']} - {p['price']} Br ({p['stock']} шт.)", 
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

# --- Обработчик кнопок ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    
    # --- Каталог ---
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
            "Он ответит тебе в этом же чате.",
            parse_mode='Markdown'
        )
    elif data == "back":
        await start(update, context)
    
    # --- Админ: товары ---
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
    elif data.startswith("change_price_"):
        product_id = int(data.replace("change_price_", ""))
        context.user_data['edit_product_id'] = product_id
        context.user_data['awaiting'] = 'change_price'
        await query.edit_message_text("💰 Введи *новую цену* (Br):", parse_mode='Markdown')
    elif data.startswith("change_stock_"):
        product_id = int(data.replace("change_stock_", ""))
        context.user_data['edit_product_id'] = product_id
        context.user_data['awaiting'] = 'change_stock'
        await query.edit_message_text("📦 Введи *новое количество*:", parse_mode='Markdown')
    elif data.startswith("change_sold_"):
        product_id = int(data.replace("change_sold_", ""))
        context.user_data['edit_product_id'] = product_id
        context.user_data['awaiting'] = 'change_sold'
        await query.edit_message_text("📊 Введи *новое количество проданных*:", parse_mode='Markdown')
    elif data == "stats":
        await show_stats(query)
    
    # --- Долги ---
    elif data.startswith("debt_"):
        product_id = int(data.replace("debt_", ""))
        context.user_data['debt_product_id'] = product_id
        context.user_data['awaiting'] = 'debt_name'
        await query.edit_message_text("👤 Введи *имя должника*:", parse_mode='Markdown')
    elif data == "debts_list":
        await show_debts(query)
    elif data.startswith("return_debt_"):
        debt_id = int(data.replace("return_debt_", ""))
        await return_debt(query, debt_id)
    
    # --- Сообщения ---
    elif data == "messages":
        await show_messages(query)
    elif data.startswith("reply_to_"):
        client_id = int(data.replace("reply_to_", ""))
        context.user_data['reply_client_id'] = client_id
        context.user_data['awaiting'] = 'reply_text'
        await query.edit_message_text(
            f"✏️ Введи *текст ответа* для клиента {client_id}:",
            parse_mode='Markdown'
        )

# --- Продажа товара ---
async def sell_product(query, product_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products WHERE id = %s", (product_id,))
    product = cur.fetchone()
    
    if not product:
        await query.answer("❌ Товар не найден")
        cur.close()
        conn.close()
        return
    
    if product['stock'] <= 0:
        await query.answer("❌ Нет в наличии!")
        cur.close()
        conn.close()
        return
    
    cur.execute("UPDATE products SET stock = stock - 1, sold = sold + 1 WHERE id = %s", (product_id,))
    conn.commit()
    cur.execute("SELECT * FROM products WHERE id = %s", (product_id,))
    updated = cur.fetchone()
    cur.close()
    conn.close()
    
    keyboard = [
        [InlineKeyboardButton("🛒 Продать (-1)", callback_data=f"sell_{product_id}")],
        [InlineKeyboardButton("📦 Пополнить (+)", callback_data=f"restock_{product_id}")],
        [InlineKeyboardButton("📝 Дать в долг", callback_data=f"debt_{product_id}")],
        [InlineKeyboardButton("💰 Изменить цену", callback_data=f"change_price_{product_id}")],
        [InlineKeyboardButton("📋 Изменить кол-во", callback_data=f"change_stock_{product_id}")],
        [InlineKeyboardButton("📊 Изменить продано", callback_data=f"change_sold_{product_id}")],
        [InlineKeyboardButton("❌ Удалить", callback_data=f"delete_product_{product_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data="my_products")],
    ]
    
    await query.edit_message_text(
        f"📦 *{updated['name']}*\n"
        f"💰 Цена: {updated['price']} Br\n"
        f"📦 Остаток: {updated['stock']} шт.\n"
        f"📊 Продано: {updated['sold']} шт.\n\n"
        f"✅ Продано! Осталось: {updated['stock']} шт.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

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
        f"💰 Цена: *{product['price']} Br*\n"
        f"📦 В наличии: *{product['stock']} шт.*{desc}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

# --- Админ: управление товарами ---
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
                f"{status} {p['name']} - {p['price']} Br ({p['stock']} шт.)",
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
        [InlineKeyboardButton("📝 Дать в долг", callback_data=f"debt_{product_id}")],
        [InlineKeyboardButton("💰 Изменить цену", callback_data=f"change_price_{product_id}")],
        [InlineKeyboardButton("📋 Изменить кол-во", callback_data=f"change_stock_{product_id}")],
        [InlineKeyboardButton("📊 Изменить продано", callback_data=f"change_sold_{product_id}")],
        [InlineKeyboardButton("❌ Удалить", callback_data=f"delete_product_{product_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data="my_products")],
    ]
    
    await query.edit_message_text(
        f"📦 *{p['name']}*\n"
        f"💰 Цена: {p['price']} Br\n"
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
    
    cur.execute("SELECT COALESCE(SUM(amount), 0) as total_debt FROM debts WHERE returned = FALSE")
    total_debt = cur.fetchone()['total_debt']
    
    cur.close()
    conn.close()
    
    await query.edit_message_text(
        "📊 *Статистика:*\n\n"
        f"📦 Всего товаров: *{total_products}*\n"
        f"📋 Остаток: *{total_stock} шт.*\n"
        f"🛒 Продано: *{total_sold} шт.*\n"
        f"💰 Выручка: *{total_revenue} Br*\n"
        f"📝 В долгу: *{total_debt} шт.*",
        reply_markup=get_admin_keyboard(),
        parse_mode='Markdown'
    )

# --- Долги ---
async def show_debts(query):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT d.*, p.name as product_name, p.price 
        FROM debts d 
        JOIN products p ON d.product_id = p.id 
        ORDER BY d.created_at DESC
    """)
    debts = cur.fetchall()
    cur.close()
    conn.close()
    
    if not debts:
        await query.edit_message_text("📝 Нет долгов.", reply_markup=get_admin_keyboard())
        return
    
    text = "📝 *Долги:*\n\n"
    keyboard = []
    total_debt = 0
    
    for d in debts:
        status = "✅" if d['returned'] else "❌"
        text += f"{status} *{d['debtor_name']}*: {d['amount']} шт. {d['product_name']} ({d['amount'] * d['price']} Br)\n"
        if not d['returned']:
            total_debt += d['amount'] * d['price']
            keyboard.append([
                InlineKeyboardButton(f"✅ Вернул: {d['debtor_name']}", callback_data=f"return_debt_{d['id']}")
            ])
    
    text += f"\n💰 *Общая сумма долга: {total_debt} Br*"
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def return_debt(query, debt_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE debts SET returned = TRUE WHERE id = %s", (debt_id,))
    conn.commit()
    cur.close()
    conn.close()
    
    await query.answer("✅ Долг погашен!")
    await show_debts(query)

async def show_messages(query):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM chat_messages WHERE from_admin = FALSE ORDER BY created_at DESC")
    messages = cur.fetchall()
    cur.close()
    conn.close()
    
    if not messages:
        await query.edit_message_text("💬 Нет сообщений.", reply_markup=get_admin_keyboard())
        return
    
    clients = {}
    for msg in messages:
        if msg['client_id'] not in clients:
            clients[msg['client_id']] = msg
    
    text = "💬 *Сообщения от клиентов:*\n\n"
    keyboard = []
    
    for client_id, msg in list(clients.items())[:8]:
        text += f"👤 *{client_id}*: {msg['message'][:40]}...\n"
        keyboard.append([
            InlineKeyboardButton(
                f"✏️ Ответить {client_id}", 
                callback_data=f"reply_to_{client_id}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

# --- Обработчик текста ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    awaiting = context.user_data.get('awaiting')
    
    # Админ: добавление товара
    if user_id == ADMIN_ID and awaiting == 'product_name':
        context.user_data['product_name'] = text
        context.user_data['awaiting'] = 'product_price'
        await update.message.reply_text("💰 Введи *цену* (Br):", parse_mode='Markdown')
    
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
            cur.execute("INSERT INTO products (name, price, stock) VALUES (%s, %s, %s)", (name, price, stock))
            conn.commit()
            cur.close()
            conn.close()
            
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ Товар *{name}* добавлен!\n💰 {price} Br | 📦 {stock} шт.",
                reply_markup=get_admin_keyboard(),
                parse_mode='Markdown'
            )
        except:
            await update.message.reply_text("❌ Введи число!")
    
    # Админ: изменение цены
    elif user_id == ADMIN_ID and awaiting == 'change_price':
        try:
            price = float(text)
            product_id = context.user_data['edit_product_id']
            
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE products SET price = %s WHERE id = %s", (price, product_id))
            conn.commit()
            cur.close()
            conn.close()
            
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ Цена изменена на *{price} Br*!",
                reply_markup=get_admin_keyboard(),
                parse_mode='Markdown'
            )
        except:
            await update.message.reply_text("❌ Введи число!")
    
    # Админ: изменение количества
    elif user_id == ADMIN_ID and awaiting == 'change_stock':
        try:
            stock = int(text)
            product_id = context.user_data['edit_product_id']
            
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE products SET stock = %s WHERE id = %s", (stock, product_id))
            conn.commit()
            cur.close()
            conn.close()
            
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ Количество изменено на *{stock} шт.*!",
                reply_markup=get_admin_keyboard(),
                parse_mode='Markdown'
            )
        except:
            await update.message.reply_text("❌ Введи число!")
    
    # Админ: изменение продано
    elif user_id == ADMIN_ID and awaiting == 'change_sold':
        try:
            sold = int(text)
            product_id = context.user_data['edit_product_id']
            
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE products SET sold = %s WHERE id = %s", (sold, product_id))
            conn.commit()
            cur.close()
            conn.close()
            
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ Количество проданных изменено на *{sold} шт.*!",
                reply_markup=get_admin_keyboard(),
                parse_mode='Markdown'
            )
        except:
            await update.message.reply_text("❌ Введи число!")
    
    # Админ: долг - имя
    elif user_id == ADMIN_ID and awaiting == 'debt_name':
        context.user_data['debt_name'] = text.strip()
        context.user_data['awaiting'] = 'debt_amount'
        await update.message.reply_text("📦 Сколько штук в долг?", parse_mode='Markdown')
    
    # Админ: долг - количество
    elif user_id == ADMIN_ID and awaiting == 'debt_amount':
        try:
            text = text.strip().replace(',', '.')
            amount = int(float(text))
            product_id = context.user_data['debt_product_id']
            debtor = context.user_data['debt_name']
            
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM products WHERE id = %s", (product_id,))
            product = cur.fetchone()
            
            if product['stock'] < amount:
                await update.message.reply_text(f"❌ Недостаточно! В наличии: {product['stock']} шт.")
                context.user_data.clear()
                cur.close()
                conn.close()
                return
            
            # Списываем товар (НЕ добавляем к sold!)
            cur.execute("UPDATE products SET stock = stock - %s WHERE id = %s", (amount, product_id))
            
            # Проверяем, есть ли уже долг для этого человека
            cur.execute("SELECT * FROM debts WHERE debtor_name = %s AND returned = FALSE", (debtor,))
            existing = cur.fetchone()
            
            if existing:
                # Увеличиваем существующий долг
                cur.execute("UPDATE debts SET amount = amount + %s WHERE id = %s", (amount, existing['id']))
            else:
                # Создаём новый долг
                cur.execute(
                    "INSERT INTO debts (product_id, debtor_name, amount) VALUES (%s, %s, %s)",
                    (product_id, debtor, amount)
                )
            
            conn.commit()
            cur.close()
            conn.close()
            
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ *{debtor}* взял в долг *{amount} шт.* {product['name']}",
                reply_markup=get_admin_keyboard(),
                parse_mode='Markdown'
            )
        except:
            await update.message.reply_text("❌ Введи число!")
    
    # Админ: ответ клиенту
    elif user_id == ADMIN_ID and awaiting == 'reply_text':
        client_id = context.user_data.get('reply_client_id')
        reply_text = text
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_messages (client_id, message, from_admin) VALUES (%s, %s, TRUE)",
            (client_id, reply_text)
        )
        conn.commit()
        cur.close()
        conn.close()
        
        try:
            await context.bot.send_message(
                client_id,
                f"📩 *Ответ от продавца:*\n\n{reply_text}",
                parse_mode='Markdown'
            )
            await update.message.reply_text(
                f"✅ Ответ отправлен клиенту {client_id}!",
                reply_markup=get_admin_keyboard(),
                parse_mode='Markdown'
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Не удалось отправить: {e}",
                reply_markup=get_admin_keyboard()
            )
        
        context.user_data.clear()
    
    # Админ: пополнение товара
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
        cur.execute("INSERT INTO chat_messages (client_id, message) VALUES (%s, %s)", (user_id, text))
        conn.commit()
        cur.close()
        conn.close()
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Ответить", callback_data=f"reply_to_{user_id}")]
        ])
        
        await context.bot.send_message(
            ADMIN_ID,
            f"💬 *Сообщение от клиента* (ID: {user_id}):\n\n{text}",
            reply_markup=keyboard,
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