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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    # Удаляем ограничение UNIQUE, если оно существует
    try:
        cur.execute("ALTER TABLE debts DROP CONSTRAINT IF EXISTS debts_debtor_name_key")
        conn.commit()
        logger.info("Ограничение UNIQUE удалено из таблицы debts")
    except:
        pass
    
    conn.commit()
    cur.close()
    conn.close()
    logger.info("База данных готова")

# --- Функция для безопасного преобразования чисел ---
def to_num(text, as_int=True):
    text = str(text).strip().replace(',', '.')
    return int(float(text)) if as_int else float(text)

# --- Клавиатуры ---
def get_client_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍 Каталог товаров", callback_data="catalog")],
        [InlineKeyboardButton("📞 Связаться с продавцом", callback_data="contact")],
    ])

def get_admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить товар", callback_data="add_product")],
        [InlineKeyboardButton("📋 Мои товары", callback_data="my_products")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("💬 Сообщения от клиентов", callback_data="messages")],
        [InlineKeyboardButton("📝 Долги", callback_data="debts_list")],
    ])

def get_catalog_keyboard(products, page=0):
    keyboard = []
    per_page = 5
    start = page * per_page
    end = start + per_page
    for p in products[start:end]:
        keyboard.append([InlineKeyboardButton(
            f"{p['name']} — {p['price']} Br ({p['stock']} шт.)",
            callback_data=f"product_{p['id']}"
        )])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("◀️", callback_data=f"catalog_page_{page-1}"))
    if end < len(products): nav.append(InlineKeyboardButton("▶️", callback_data=f"catalog_page_{page+1}"))
    if nav: keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Главное меню", callback_data="back")])
    return InlineKeyboardMarkup(keyboard)

# --- Команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin = user_id == ADMIN_ID
    text = "🔐 *Админ-панель*\n\nВыбери действие:" if is_admin else "👋 *Добро пожаловать!*\n\nЯ бот для заказа товаров.\nВыбери действие:"
    keyboard = get_admin_keyboard() if is_admin else get_client_keyboard()
    if update.message:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode='Markdown')
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')

# --- Обработчик кнопок ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "catalog": await show_catalog(query)
    elif data.startswith("catalog_page_"): await show_catalog(query, int(data.split("_")[-1]))
    elif data.startswith("product_"): await show_product(query, int(data.split("_")[-1]))
    elif data == "contact": await query.edit_message_text("📞 Напиши сообщение сюда, я передам продавцу.")
    elif data == "back": await start(update, context)
    
    elif data == "add_product":
        context.user_data['awaiting'] = 'product_name'
        await query.edit_message_text("✏️ Введи *название товара*:", parse_mode='Markdown')
    elif data == "my_products": await show_admin_products(query)
    elif data.startswith("admin_products_page_"): await show_admin_products(query, int(data.split("_")[-1]))
    elif data.startswith("edit_product_"): await show_edit_menu(query, int(data.split("_")[-1]))
    elif data.startswith("delete_product_"): await delete_product(query, int(data.split("_")[-1]))
    elif data.startswith("restock_"):
        context.user_data['restock_product_id'] = int(data.split("_")[-1])
        context.user_data['awaiting'] = 'restock_amount'
        await query.edit_message_text("📦 Введи количество для пополнения:")
    elif data.startswith("sell_"): await sell_product(query, int(data.split("_")[-1]))
    elif data.startswith("change_price_"):
        context.user_data['edit_product_id'] = int(data.split("_")[-1])
        context.user_data['awaiting'] = 'change_price'
        await query.edit_message_text("💰 Введи *новую цену* (Br):", parse_mode='Markdown')
    elif data.startswith("change_stock_"):
        context.user_data['edit_product_id'] = int(data.split("_")[-1])
        context.user_data['awaiting'] = 'change_stock'
        await query.edit_message_text("📦 Введи *новое количество*:")
    elif data.startswith("change_sold_"):
        context.user_data['edit_product_id'] = int(data.split("_")[-1])
        context.user_data['awaiting'] = 'change_sold'
        await query.edit_message_text("📊 Введи *новое проданных*:")
    elif data == "stats": await show_stats(query)
    
    elif data.startswith("debt_"):
        context.user_data['debt_product_id'] = int(data.split("_")[-1])
        context.user_data['awaiting'] = 'debt_name'
        await query.edit_message_text("👤 Введи *имя должника*:", parse_mode='Markdown')
    elif data == "debts_list": await show_debts(query)
    elif data.startswith("return_debt_"): await return_debt(query, int(data.split("_")[-1]))
    
    elif data == "messages": await show_messages(query)
    elif data.startswith("reply_to_"):
        context.user_data['reply_client_id'] = int(data.split("_")[-1])
        context.user_data['awaiting'] = 'reply_text'
        await query.edit_message_text(f"✏️ Введи *текст ответа* для клиента {data.split('_')[-1]}:", parse_mode='Markdown')

# --- Продажа ---
async def sell_product(query, pid):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE products SET stock=stock-1, sold=sold+1 WHERE id=%s AND stock>0", (pid,))
    conn.commit()
    cur.close()
    conn.close()
    await query.answer("✅ Продано!")
    await show_edit_menu(query, pid)

# --- Показ ---
async def show_catalog(query, page=0):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products WHERE active=TRUE AND stock>0 ORDER BY name")
    products = cur.fetchall()
    cur.close()
    conn.close()
    if not products:
        await query.edit_message_text("📭 Нет товаров.", reply_markup=get_client_keyboard())
        return
    await query.edit_message_text("🛍 *Каталог:*", reply_markup=get_catalog_keyboard(products, page), parse_mode='Markdown')

async def show_product(query, pid):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products WHERE id=%s", (pid,))
    p = cur.fetchone()
    cur.close()
    conn.close()
    if not p: await query.answer("Не найден"); return
    await query.edit_message_text(
        f"📦 *{p['name']}*\n\n💰 {p['price']} Br\n📦 {p['stock']} шт.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📞 Связаться", callback_data="contact")],
            [InlineKeyboardButton("🔙 Назад", callback_data="catalog")]
        ]),
        parse_mode='Markdown'
    )

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
        keyboard.append([InlineKeyboardButton(
            f"{'✅' if p['active'] else '❌'} {p['name']} — {p['price']} Br ({p['stock']} шт.)",
            callback_data=f"edit_product_{p['id']}"
        )])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("◀️", callback_data=f"admin_products_page_{page-1}"))
    if end < len(products): nav.append(InlineKeyboardButton("▶️", callback_data=f"admin_products_page_{page+1}"))
    if nav: keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    await query.edit_message_text("📋 *Товары:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def show_edit_menu(query, pid):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products WHERE id=%s", (pid,))
    p = cur.fetchone()
    cur.close()
    conn.close()
    keyboard = [
        [InlineKeyboardButton("🛒 Продать (-1)", callback_data=f"sell_{pid}")],
        [InlineKeyboardButton("📦 Пополнить (+)", callback_data=f"restock_{pid}")],
        [InlineKeyboardButton("📝 Дать в долг", callback_data=f"debt_{pid}")],
        [InlineKeyboardButton("💰 Изменить цену", callback_data=f"change_price_{pid}")],
        [InlineKeyboardButton("📋 Изменить кол-во", callback_data=f"change_stock_{pid}")],
        [InlineKeyboardButton("📊 Изменить продано", callback_data=f"change_sold_{pid}")],
        [InlineKeyboardButton("❌ Удалить", callback_data=f"delete_product_{pid}")],
        [InlineKeyboardButton("🔙 Назад", callback_data="my_products")],
    ]
    await query.edit_message_text(
        f"📦 *{p['name']}*\n💰 {p['price']} Br\n📦 {p['stock']} шт.\n📊 Продано: {p['sold']} шт.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
    )

async def delete_product(query, pid):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM products WHERE id=%s", (pid,))
    conn.commit()
    cur.close()
    conn.close()
    await query.answer("✅ Удалено!")
    await show_admin_products(query)

async def show_stats(query):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT COUNT(*) as total FROM products WHERE active=TRUE")
    total = cur.fetchone()['total']
    cur.execute("SELECT COALESCE(SUM(stock),0) FROM products WHERE active=TRUE")
    stock = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(sold),0) FROM products")
    sold = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(sold*price),0) FROM products")
    rev = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(d.amount*p.price),0) FROM debts d JOIN products p ON d.product_id=p.id WHERE d.returned=FALSE")
    debt = cur.fetchone()[0]
    cur.close()
    conn.close()
    await query.edit_message_text(
        f"📊 *Статистика:*\n\n📦 Товаров: *{total}*\n📋 Остаток: *{stock} шт.*\n🛒 Продано: *{sold} шт.*\n💰 Выручка: *{rev} Br*\n📝 В долгу: *{debt} Br*",
        reply_markup=get_admin_keyboard(), parse_mode='Markdown'
    )

async def show_debts(query):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT d.id, d.debtor_name, d.amount, d.returned, p.name, p.price FROM debts d JOIN products p ON d.product_id=p.id ORDER BY d.created_at DESC")
    debts = cur.fetchall()
    cur.close()
    conn.close()
    if not debts:
        await query.edit_message_text("📝 Нет долгов.", reply_markup=get_admin_keyboard())
        return
    text, keyboard, total = "📝 *Долги:*\n\n", [], 0
    for d in debts:
        did, name, amt, ret, pname, price = d
        if not ret:
            total += amt * price
            text += f"❌ *{name}*: {amt} шт. {pname} ({amt * price} Br)\n"
            keyboard.append([InlineKeyboardButton(f"🟢 Вернул: {name}", callback_data=f"return_debt_{did}")])
        else:
            text += f"✅ *{name}*: {amt} шт. {pname} ({amt * price} Br)\n"
    if total: text += f"\n💰 *Общий долг: {total} Br*"
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def return_debt(query, did):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM debts WHERE id=%s", (did,))
    d = cur.fetchone()
    if d:
        cur.execute("UPDATE debts SET returned=TRUE WHERE id=%s", (did,))
        cur.execute("UPDATE products SET sold=sold+%s WHERE id=%s", (d['amount'], d['product_id']))
        conn.commit()
    cur.close()
    conn.close()
    await query.answer("✅ Погашен!")
    await show_debts(query)

async def show_messages(query):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM chat_messages WHERE from_admin=FALSE ORDER BY created_at DESC")
    msgs = cur.fetchall()
    cur.close()
    conn.close()
    if not msgs:
        await query.edit_message_text("💬 Нет сообщений.", reply_markup=get_admin_keyboard())
        return
    seen, keyboard = set(), []
    text = "💬 *Сообщения:*\n\n"
    for m in msgs:
        if m['client_id'] not in seen:
            seen.add(m['client_id'])
            text += f"👤 *{m['client_id']}*: {m['message'][:40]}...\n"
            keyboard.append([InlineKeyboardButton(f"✏️ Ответить {m['client_id']}", callback_data=f"reply_to_{m['client_id']}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# --- Обработчик текста ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    awaiting = context.user_data.get('awaiting')
    
    if user_id != ADMIN_ID:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO chat_messages (client_id, message) VALUES (%s, %s)", (user_id, text))
        conn.commit()
        cur.close()
        conn.close()
        await update.message.reply_text("✅ Отправлено!", reply_markup=get_client_keyboard())
        await context.bot.send_message(ADMIN_ID, f"💬 *Клиент {user_id}:*\n{text}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Ответить", callback_data=f"reply_to_{user_id}")]]), parse_mode='Markdown')
        return
    
    try:
        if awaiting == 'product_name':
            context.user_data['product_name'] = text
            context.user_data['awaiting'] = 'product_price'
            await update.message.reply_text("💰 Цена (Br):")
        elif awaiting == 'product_price':
            context.user_data['product_price'] = to_num(text, False)
            context.user_data['awaiting'] = 'product_stock'
            await update.message.reply_text("📦 Количество:")
        elif awaiting == 'product_stock':
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("INSERT INTO products (name, price, stock) VALUES (%s, %s, %s)", (context.user_data['product_name'], context.user_data['product_price'], to_num(text)))
            conn.commit(); cur.close(); conn.close()
            name = context.user_data['product_name']
            context.user_data.clear()
            await update.message.reply_text(f"✅ *{name}* добавлен!", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        elif awaiting == 'change_price':
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE products SET price=%s WHERE id=%s", (to_num(text, False), context.user_data['edit_product_id']))
            conn.commit(); cur.close(); conn.close()
            context.user_data.clear()
            await update.message.reply_text(f"✅ Цена: *{to_num(text, False)} Br*", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        elif awaiting == 'change_stock':
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE products SET stock=%s WHERE id=%s", (to_num(text), context.user_data['edit_product_id']))
            conn.commit(); cur.close(); conn.close()
            context.user_data.clear()
            await update.message.reply_text(f"✅ Остаток: *{to_num(text)} шт.*", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        elif awaiting == 'change_sold':
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE products SET sold=%s WHERE id=%s", (to_num(text), context.user_data['edit_product_id']))
            conn.commit(); cur.close(); conn.close()
            context.user_data.clear()
            await update.message.reply_text(f"✅ Продано: *{to_num(text)} шт.*", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        elif awaiting == 'debt_name':
            context.user_data['debt_name'] = text
            context.user_data['awaiting'] = 'debt_amount'
            await update.message.reply_text("📦 Сколько штук в долг?")
        elif awaiting == 'debt_amount':
            amt = to_num(text)
            pid = context.user_data.get('debt_product_id')
            debtor = context.user_data.get('debt_name', '')
            if not pid:
                await update.message.reply_text("❌ Ошибка: товар не выбран", reply_markup=get_admin_keyboard())
                context.user_data.clear()
                return
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM products WHERE id=%s", (pid,))
            p = cur.fetchone()
            if not p:
                await update.message.reply_text("❌ Товар не найден", reply_markup=get_admin_keyboard())
            elif p['stock'] < amt:
                await update.message.reply_text(f"❌ Недостаточно! В наличии: {p['stock']} шт.", reply_markup=get_admin_keyboard())
            else:
                cur.execute("UPDATE products SET stock=stock-%s WHERE id=%s", (amt, pid))
                cur.execute("INSERT INTO debts (product_id, debtor_name, amount) VALUES (%s, %s, %s)", (pid, debtor, amt))
                conn.commit()
                await update.message.reply_text(f"✅ *{debtor}* взял *{amt} шт.* {p['name']}", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
            cur.close(); conn.close()
            context.user_data.clear()
        elif awaiting == 'restock_amount':
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE products SET stock=stock+%s WHERE id=%s", (to_num(text), context.user_data['restock_product_id']))
            conn.commit(); cur.close(); conn.close()
            context.user_data.clear()
            await update.message.reply_text(f"✅ Пополнено на *{to_num(text)} шт.*", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        elif awaiting == 'reply_text':
            cid = context.user_data['reply_client_id']
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("INSERT INTO chat_messages (client_id, message, from_admin) VALUES (%s, %s, TRUE)", (cid, text))
            conn.commit(); cur.close(); conn.close()
            context.user_data.clear()
            try:
                await context.bot.send_message(cid, f"📩 *Ответ продавца:*\n\n{text}", parse_mode='Markdown')
                await update.message.reply_text(f"✅ Отправлено клиенту {cid}!", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
            except:
                await update.message.reply_text("❌ Не удалось отправить", reply_markup=get_admin_keyboard())
    except (ValueError, TypeError):
        await update.message.reply_text("❌ Введи число!")

@app.route('/health')
def health():
    return "OK", 200

def main():
    if not TOKEN: return
    init_db()
    bot = Application.builder().token(TOKEN).build()
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(CallbackQueryHandler(button_handler))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Бот запущен!")
    import threading
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)), debug=False, use_reloader=False), daemon=True).start()
    bot.run_polling()

if __name__ == "__main__":
    main()