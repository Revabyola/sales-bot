import os
import logging
from datetime import datetime, date
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
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sales_history (
            id SERIAL PRIMARY KEY,
            product_name TEXT NOT NULL,
            price REAL NOT NULL,
            sold_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    try:
        cur.execute("ALTER TABLE debts DROP CONSTRAINT IF EXISTS debts_debtor_name_key")
        conn.commit()
    except:
        pass
    
    conn.commit()
    cur.close()
    conn.close()
    logger.info("База данных готова")

def to_num(text, as_int=True):
    text = str(text).strip().replace(',', '.')
    return int(float(text)) if as_int else float(text)

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
        [InlineKeyboardButton("📅 История продаж", callback_data="history")],
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

async def start(update, ctx):
    uid = update.effective_user.id
    is_admin = uid == ADMIN_ID
    text = "🔐 *Админ-панель*\n\nВыбери действие:" if is_admin else "👋 *Добро пожаловать!*\n\nЯ бот для заказа товаров.\nВыбери действие:"
    keyboard = get_admin_keyboard() if is_admin else get_client_keyboard()
    if update.message:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode='Markdown')
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')

async def button_handler(update, ctx):
    q = update.callback_query
    await q.answer()
    d = q.data
    
    if d == "catalog": await show_catalog(q)
    elif d.startswith("catalog_page_"): await show_catalog(q, int(d.split("_")[-1]))
    elif d.startswith("product_"): await show_product(q, int(d.split("_")[-1]))
    elif d == "contact": await q.edit_message_text("📞 Напиши сообщение сюда, я передам продавцу.")
    elif d == "back": await start(update, ctx)
    
    elif d == "add_product":
        ctx.user_data['awaiting'] = 'product_name'
        await q.edit_message_text("✏️ Введи *название товара*:", parse_mode='Markdown')
    elif d == "my_products": await show_admin_products(q)
    elif d.startswith("edit_product_"): await show_edit_menu(q, int(d.split("_")[-1]))
    elif d.startswith("delete_product_"): await delete_product(q, int(d.split("_")[-1]))
    elif d.startswith("restock_"):
        ctx.user_data['restock_product_id'] = int(d.split("_")[-1])
        ctx.user_data['awaiting'] = 'restock_amount'
        await q.edit_message_text("📦 Введи количество для пополнения:")
    elif d.startswith("sell_"): await sell_product(q, int(d.split("_")[-1]))
    elif d.startswith("change_price_"):
        ctx.user_data['edit_product_id'] = int(d.split("_")[-1])
        ctx.user_data['awaiting'] = 'change_price'
        await q.edit_message_text("💰 Введи *новую цену* (Br):", parse_mode='Markdown')
    elif d.startswith("change_stock_"):
        ctx.user_data['edit_product_id'] = int(d.split("_")[-1])
        ctx.user_data['awaiting'] = 'change_stock'
        await q.edit_message_text("📦 Введи *новое количество*:")
    elif d.startswith("change_sold_"):
        ctx.user_data['edit_product_id'] = int(d.split("_")[-1])
        ctx.user_data['awaiting'] = 'change_sold'
        await q.edit_message_text("📊 Введи *новое проданных*:")
    elif d == "stats": await show_stats(q)
    elif d == "history": await show_history(q)
    elif d.startswith("day_"): await show_day_stats(q, d.split("_")[1])
    
    elif d.startswith("debt_"):
        ctx.user_data['debt_product_id'] = int(d.split("_")[-1])
        ctx.user_data['awaiting'] = 'debt_name'
        await q.edit_message_text("👤 Введи *имя должника*:", parse_mode='Markdown')
    elif d == "debts_list": await show_debts(q)
    elif d.startswith("return_debt_"): await return_debt(q, int(d.split("_")[-1]))
    
    elif d == "messages": await show_messages(q)
    elif d.startswith("reply_to_"):
        ctx.user_data['reply_client_id'] = int(d.split("_")[-1])
        ctx.user_data['awaiting'] = 'reply_text'
        await q.edit_message_text(f"✏️ Введи *текст ответа* для клиента {d.split('_')[-1]}:", parse_mode='Markdown')

async def sell_product(q, pid):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products WHERE id=%s", (pid,))
    p = cur.fetchone()
    
    if not p or p['stock'] <= 0:
        await q.answer("❌ Нет в наличии!")
        cur.close()
        conn.close()
        return
    
    cur.execute("UPDATE products SET stock=stock-1, sold=sold+1 WHERE id=%s", (pid,))
    cur.execute("INSERT INTO sales_history (product_name, price) VALUES (%s, %s)", (p['name'], p['price']))
    conn.commit()
    cur.close()
    conn.close()
    await q.answer("✅ Продано!")
    await show_edit_menu(q, pid)

async def show_catalog(q, page=0):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products WHERE active=TRUE AND stock>0 ORDER BY name")
    p = cur.fetchall(); cur.close()
    if not p: await q.edit_message_text("📭 Нет товаров.", reply_markup=get_client_keyboard()); return
    await q.edit_message_text("🛍 *Каталог:*", reply_markup=get_catalog_keyboard(p, page), parse_mode='Markdown')

async def show_product(q, pid):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products WHERE id=%s", (pid,))
    p = cur.fetchone(); cur.close()
    if not p: await q.answer("Не найден"); return
    await q.edit_message_text(f"📦 *{p['name']}*\n\n💰 {p['price']} Br\n📦 {p['stock']} шт.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📞 Связаться", callback_data="contact")], [InlineKeyboardButton("🔙 Назад", callback_data="catalog")]]), parse_mode='Markdown')

async def show_admin_products(q):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products ORDER BY name")
    p = cur.fetchall(); cur.close()
    if not p: await q.edit_message_text("📭 Нет товаров.", reply_markup=get_admin_keyboard()); return
    kb = [[InlineKeyboardButton(f"{'✅' if r['active'] else '❌'} {r['name']} — {r['price']} Br ({r['stock']} шт.)", callback_data=f"edit_product_{r['id']}")] for r in p[:10]]
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    await q.edit_message_text("📋 *Товары:*", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def show_edit_menu(q, pid):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products WHERE id=%s", (pid,))
    p = cur.fetchone(); cur.close()
    kb = [
        [InlineKeyboardButton("🛒 Продать (-1)", callback_data=f"sell_{pid}")],
        [InlineKeyboardButton("📦 Пополнить (+)", callback_data=f"restock_{pid}")],
        [InlineKeyboardButton("📝 Дать в долг", callback_data=f"debt_{pid}")],
        [InlineKeyboardButton("💰 Изменить цену", callback_data=f"change_price_{pid}")],
        [InlineKeyboardButton("📋 Изменить кол-во", callback_data=f"change_stock_{pid}")],
        [InlineKeyboardButton("📊 Изменить продано", callback_data=f"change_sold_{pid}")],
        [InlineKeyboardButton("❌ Удалить", callback_data=f"delete_product_{pid}")],
        [InlineKeyboardButton("🔙 Назад", callback_data="my_products")],
    ]
    await q.edit_message_text(f"📦 *{p['name']}*\n💰 {p['price']} Br\n📦 {p['stock']} шт.\n📊 Продано: {p['sold']} шт.", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def delete_product(q, pid):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM products WHERE id=%s", (pid,))
    conn.commit(); cur.close()
    await q.answer("✅ Удалено!")
    await show_admin_products(q)

async def show_stats(q):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM products WHERE active=TRUE"); total = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(stock),0) FROM products WHERE active=TRUE"); stock = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(sold),0) FROM products"); sold = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(sold*price),0) FROM products"); rev = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(d.amount*p.price),0) FROM debts d JOIN products p ON d.product_id=p.id WHERE d.returned=FALSE"); debt = cur.fetchone()[0]
    cur.close(); conn.close()
    await q.edit_message_text(f"📊 *Статистика:*\n\n📦 Товаров: *{total}*\n📋 Остаток: *{stock} шт.*\n🛒 Продано: *{sold} шт.*\n💰 Выручка: *{rev} Br*\n📝 В долгу: *{debt} Br*", reply_markup=get_admin_keyboard(), parse_mode='Markdown')

async def show_history(q):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT DATE(sold_at) as day FROM sales_history ORDER BY day DESC LIMIT 7")
    days = cur.fetchall(); cur.close()
    if not days: await q.edit_message_text("📅 Нет истории продаж.", reply_markup=get_admin_keyboard()); return
    kb = [[InlineKeyboardButton(str(d[0]), callback_data=f"day_{d[0]}")] for d in days]
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    await q.edit_message_text("📅 *Выбери день:*", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def show_day_stats(q, day):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT product_name, COUNT(*) as cnt, SUM(price) as total FROM sales_history WHERE DATE(sold_at)=%s GROUP BY product_name ORDER BY cnt DESC", (day,))
    items = cur.fetchall(); cur.close()
    text = f"📅 *Продажи за {day}:*\n\n"
    total = 0
    for name, cnt, sm in items:
        text += f"• {name}: {cnt} шт. ({sm} Br)\n"
        total += sm
    text += f"\n💰 *Итого: {total} Br*"
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="history")]]), parse_mode='Markdown')

async def show_debts(q):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT d.id, d.debtor_name, d.amount, d.returned, p.name, p.price, d.created_at FROM debts d JOIN products p ON d.product_id=p.id ORDER BY d.created_at DESC")
    debts = cur.fetchall(); cur.close()
    if not debts: await q.edit_message_text("📝 Нет долгов.", reply_markup=get_admin_keyboard()); return
    text, kb, total = "📝 *Долги:*\n\n", [], 0
    for d in debts:
        did, name, amt, ret, pname, price, dt = d
        dt_str = dt.strftime('%d.%m.%Y %H:%M') if dt else ''
        if not ret:
            total += amt * price
            text += f"❌ *{name}*: {amt} шт. {pname} ({amt * price} Br)\n📅 {dt_str}\n\n"
            kb.append([InlineKeyboardButton(f"🟢 Вернул: {name}", callback_data=f"return_debt_{did}")])
        else:
            text += f"✅ *{name}*: {amt} шт. {pname} ({amt * price} Br)\n📅 {dt_str}\n\n"
    if total: text += f"💰 *Общий долг: {total} Br*"
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def return_debt(q, did):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM debts WHERE id=%s", (did,))
    d = cur.fetchone()
    if d:
        cur.execute("UPDATE debts SET returned=TRUE WHERE id=%s", (did,))
        cur.execute("UPDATE products SET sold=sold+%s WHERE id=%s", (d['amount'], d['product_id']))
        conn.commit()
    cur.close(); conn.close()
    await q.answer("✅ Погашен!")
    await show_debts(q)

async def show_messages(q):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM chat_messages WHERE from_admin=FALSE ORDER BY created_at DESC")
    msgs = cur.fetchall(); cur.close()
    if not msgs: await q.edit_message_text("💬 Нет сообщений.", reply_markup=get_admin_keyboard()); return
    seen, kb = set(), []
    text = "💬 *Сообщения:*\n\n"
    for m in msgs:
        if m['client_id'] not in seen:
            seen.add(m['client_id'])
            text += f"👤 *{m['client_id']}*: {m['message'][:40]}...\n"
            kb.append([InlineKeyboardButton(f"✏️ Ответить {m['client_id']}", callback_data=f"reply_to_{m['client_id']}")])
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def handle_text(update, ctx):
    uid = update.effective_user.id
    text = update.message.text.strip()
    awaiting = ctx.user_data.get('awaiting')
    
    if uid != ADMIN_ID:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO chat_messages (client_id, message) VALUES (%s, %s)", (uid, text))
        conn.commit(); cur.close()
        await update.message.reply_text("✅ Отправлено!", reply_markup=get_client_keyboard())
        await ctx.bot.send_message(ADMIN_ID, f"💬 *Клиент {uid}:*\n{text}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Ответить", callback_data=f"reply_to_{uid}")]]), parse_mode='Markdown')
        return
    
    try:
        if awaiting == 'product_name':
            ctx.user_data['product_name'] = text
            ctx.user_data['awaiting'] = 'product_price'
            await update.message.reply_text("💰 Цена (Br):")
        elif awaiting == 'product_price':
            ctx.user_data['product_price'] = to_num(text, False)
            ctx.user_data['awaiting'] = 'product_stock'
            await update.message.reply_text("📦 Количество:")
        elif awaiting == 'product_stock':
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("INSERT INTO products (name, price, stock) VALUES (%s, %s, %s)", (ctx.user_data['product_name'], ctx.user_data['product_price'], to_num(text)))
            conn.commit(); cur.close(); conn.close()
            name = ctx.user_data['product_name']
            ctx.user_data.clear()
            await update.message.reply_text(f"✅ *{name}* добавлен!", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        elif awaiting == 'change_price':
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE products SET price=%s WHERE id=%s", (to_num(text, False), ctx.user_data['edit_product_id']))
            conn.commit(); cur.close(); conn.close()
            ctx.user_data.clear()
            await update.message.reply_text(f"✅ Цена: *{to_num(text, False)} Br*", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        elif awaiting == 'change_stock':
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE products SET stock=%s WHERE id=%s", (to_num(text), ctx.user_data['edit_product_id']))
            conn.commit(); cur.close(); conn.close()
            ctx.user_data.clear()
            await update.message.reply_text(f"✅ Остаток: *{to_num(text)} шт.*", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        elif awaiting == 'change_sold':
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE products SET sold=%s WHERE id=%s", (to_num(text), ctx.user_data['edit_product_id']))
            conn.commit(); cur.close(); conn.close()
            ctx.user_data.clear()
            await update.message.reply_text(f"✅ Продано: *{to_num(text)} шт.*", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        elif awaiting == 'debt_name':
            ctx.user_data['debt_name'] = text
            ctx.user_data['awaiting'] = 'debt_amount'
            await update.message.reply_text("📦 Сколько штук в долг?")
        elif awaiting == 'debt_amount':
            amt = to_num(text)
            pid = ctx.user_data.get('debt_product_id')
            debtor = ctx.user_data.get('debt_name', '')
            if not pid:
                await update.message.reply_text("❌ Ошибка: товар не выбран", reply_markup=get_admin_keyboard())
                ctx.user_data.clear()
                return
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM products WHERE id=%s", (pid,))
            p = cur.fetchone()
            if not p: await update.message.reply_text("❌ Товар не найден", reply_markup=get_admin_keyboard())
            elif p['stock'] < amt: await update.message.reply_text(f"❌ Недостаточно! В наличии: {p['stock']} шт.", reply_markup=get_admin_keyboard())
            else:
                cur.execute("UPDATE products SET stock=stock-%s WHERE id=%s", (amt, pid))
                cur.execute("INSERT INTO debts (product_id, debtor_name, amount) VALUES (%s, %s, %s)", (pid, debtor, amt))
                conn.commit()
                await update.message.reply_text(f"✅ *{debtor}* взял *{amt} шт.* {p['name']}", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
            cur.close(); conn.close()
            ctx.user_data.clear()
        elif awaiting == 'restock_amount':
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("UPDATE products SET stock=stock+%s WHERE id=%s", (to_num(text), ctx.user_data['restock_product_id']))
            conn.commit(); cur.close(); conn.close()
            ctx.user_data.clear()
            await update.message.reply_text(f"✅ Пополнено на *{to_num(text)} шт.*", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        elif awaiting == 'reply_text':
            cid = ctx.user_data['reply_client_id']
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("INSERT INTO chat_messages (client_id, message, from_admin) VALUES (%s, %s, TRUE)", (cid, text))
            conn.commit(); cur.close(); conn.close()
            ctx.user_data.clear()
            try:
                await ctx.bot.send_message(cid, f"📩 *Ответ продавца:*\n\n{text}", parse_mode='Markdown')
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