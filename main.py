import logging
import os
import threading
import asyncio
import psycopg2
from flask import Flask, request
from datetime import datetime, timedelta

# v20+ অনুযায়ী ইম্পোর্ট স্টেটমেন্ট
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeChat
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)

# লগিং সিস্টেম সেটআপ
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Database Setup (Supabase / PostgreSQL) ---
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    """ডেটাবেস কানেকশন স্থাপন করে"""
    if not DATABASE_URL:
        logger.error("DATABASE_URL variable missing!")
        return None
    try:
        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url, sslmode='require', connect_timeout=10)
        return conn
    except Exception as e:
        logger.error(f"DB Error: {e}")
        return None

# ------------------- কনফিগারেশন -------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", 0))

# কথোপকথনের ধাপ (States)
GET_MEDIA, GET_TITLE, GET_CUSTOM_CODE, GET_BROADCAST_MSG = range(4)

def init_db():
    """প্রয়োজনীয় টেবিল এবং কলাম তৈরি বা অটো-আপডেট করে"""
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT PRIMARY KEY,
                        username TEXT,
                        full_name TEXT,
                        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT")
                
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS app_logs (
                        user_id BIGINT,
                        last_open TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cur.execute("ALTER TABLE app_logs ADD COLUMN IF NOT EXISTS last_open TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS files (
                        custom_code TEXT PRIMARY KEY,
                        title TEXT,
                        file_type TEXT,
                        file_id TEXT
                    )
                """)
                conn.commit()
                logger.info("Database initialized successfully.")
        except Exception as e: logger.error(f"DB Init Error: {e}")
        finally: conn.close()

def save_user(user_id, username, full_name):
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, username, full_name, joined_at) VALUES (%s, %s, %s, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, full_name = EXCLUDED.full_name",
                    (user_id, username, full_name)
                )
                conn.commit()
        except Exception as e: logger.error(f"Save User Error: {e}")
        finally: conn.close()

def track_app_open(user_id):
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(last_open) FROM app_logs WHERE user_id = %s", (user_id,))
                res = cur.fetchone()
                now = datetime.now()
                if res[0] is None or (now - res[0] >= timedelta(hours=24)):
                    cur.execute("INSERT INTO app_logs (user_id, last_open) VALUES (%s, %s)", (user_id, now))
                    conn.commit()
        except Exception as e: logger.error(f"Track App Error: {e}")
        finally: conn.close()

async def post_init(application: Application):
    init_db()
    user_commands = [BotCommand("start", "বট শুরু করুন")]
    await application.bot.set_my_commands(user_commands)
    if ADMIN_USER_ID:
        admin_commands = [
            BotCommand("start", "বট শুরু করুন"),
            BotCommand("alllink", "সব ফাইলের তালিকা"),
            BotCommand("broadcast", "ব্রডকাস্ট (টেক্সট/মিডিয়া)"),
            BotCommand("statics", "বটের পরিসংখ্যান"),
            BotCommand("cancel", "বর্তমান কাজ বাতিল")
        ]
        try:
            await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_USER_ID))
        except Exception as e: logger.error(f"Menu Error: {e}")

# --- বট হ্যান্ডলারসমূহ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    save_user(user.id, user.username, user.full_name)
    
    if context.args:
        file_code = context.args[0]
        conn = get_db_connection()
        if not conn: return
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT file_type, file_id, title FROM files WHERE custom_code = %s", (file_code,))
                res = cur.fetchone()
            if res:
                f_types, f_ids, title = res
                await context.bot.send_message(chat_id=user.id, text=f"*{title}*", parse_mode='Markdown')
                
                ids_list = f_ids.split('|')
                types_list = f_types.split('|')
                
                for fid, ftype in zip(ids_list, types_list):
                    try:
                        if ftype == 'text': await context.bot.send_message(chat_id=user.id, text=fid, protect_content=True)
                        elif ftype == 'video': await context.bot.send_video(chat_id=user.id, video=fid, protect_content=True)
                        elif ftype == 'document': await context.bot.send_document(chat_id=user.id, document=fid, protect_content=True)
                        elif ftype == 'audio': await context.bot.send_audio(chat_id=user.id, audio=fid, protect_content=True)
                        elif ftype == 'photo': await context.bot.send_photo(chat_id=user.id, photo=fid, protect_content=True)
                        await asyncio.sleep(0.3)
                    except: continue
        finally: conn.close()
    else:
        await update.message.reply_text(f"স্বাগতম {user.first_name} 😎 এই বটে আপনি নিয়মিত নতুন লিংকের আপডেট পাবেন। বটের সাথেই থাকুন এবং সকল সেলিব্রিটির লিংক এবং ভাইরাল ভিডিও গুলো ইনজয় করুন।")

async def statics_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """সুন্দর ও রেসপন্সিভ পরিসংখ্যান টেমপ্লেট"""
    if update.effective_user.id != ADMIN_USER_ID: return
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            total_users = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM users WHERE joined_at >= CURRENT_DATE")
            today_users = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM app_logs")
            total_app_opens = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM app_logs WHERE last_open >= (NOW() - INTERVAL '24 HOURS')")
            today_app_opens = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM files")
            total_links = cur.fetchone()[0]
            
            stats_msg = (
                "📊 **বট ব্যবহারের পরিসংখ্যান**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 **ইউজার রিপোর্ট**\n"
                f"├ আজকের নতুন: `{today_users}`\n"
                f"└ মোট ইউজার: `{total_users}`\n\n"
                f"📱 **মিনি অ্যাপ রিপোর্ট**\n"
                f"├ গত ২৪ ঘণ্টায়: `{today_app_opens}`\n"
                f"└ মোট ওপেন: `{total_app_opens}`\n\n"
                f"🔗 **লিঙ্ক রিপোর্ট**\n"
                f"└ মোট তৈরি লিঙ্ক: `{total_links}`\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📅 তারিখ: {datetime.now().strftime('%d %B, %Y')}"
            )
            await update.message.reply_text(stats_msg, parse_mode='Markdown')
    finally: conn.close()

# --- উন্নত ব্রডকাস্ট লজিক ---

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_USER_ID: return ConversationHandler.END
    await update.message.reply_text("📢 **ব্রডকাস্ট শুরু করুন**\n\nযে মেসেজ বা মিডিয়াটি পাঠাতে চান তা দিন। বাতিল করতে /cancel লিখুন।", parse_mode='Markdown')
    return GET_BROADCAST_MSG

async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin_msg = update.message
    conn = get_db_connection()
    if not conn: return ConversationHandler.END
    
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users")
            users = cur.fetchall()
        
        total = len(users)
        progress_msg = await update.message.reply_text(f"⏳ ব্রডকাস্টিং শুরু হয়েছে... (০/{total})")
        success = 0
        
        for index, (u_id,) in enumerate(users, 1):
            try:
                await context.bot.copy_message(
                    chat_id=u_id, 
                    from_chat_id=admin_msg.chat_id, 
                    message_id=admin_msg.message_id, 
                    protect_content=True
                )
                success += 1
                # প্রতি ১০ জন পাঠানোর পর প্রগ্রেস আপডেট
                if index % 10 == 0:
                    await progress_msg.edit_text(f"⏳ ব্রডকাস্টিং চলছে... ({index}/{total})")
                await asyncio.sleep(0.05)
            except: continue
            
        await progress_msg.edit_text(f"✅ **ব্রডকাস্ট সম্পন্ন!**\n\n📊 ফলাফল:\n├ মোট ইউজার: `{total}`\n└ সফলভাবে পাঠানো হয়েছে: `{success}` জন।", parse_mode='Markdown')
    finally: conn.close()
    return ConversationHandler.END

async def all_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_USER_ID: return
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT custom_code, title FROM files")
            results = cur.fetchall()
        if results:
            keyboard = [[InlineKeyboardButton(t or c, callback_data=c)] for c, t in results]
            await update.message.reply_text('📂 **সব লিঙ্কের তালিকা:**', reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    finally: conn.close()

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    bot_info = await context.bot.get_me()
    await query.message.reply_text(f"🔗 লিঙ্ক: `https://t.me/{bot_info.username}?start={query.data}`", parse_mode='Markdown')

# --- মাল্টিপল ফাইল কালেকশন ---

async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_USER_ID: return ConversationHandler.END
    context.user_data['multi_files'] = []
    return await add_to_media_list(update, context)

async def add_to_media_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    f_id, f_type = None, None
    
    if msg.video: f_id, f_type = msg.video.file_id, 'video'
    elif msg.document: f_id, f_type = msg.document.file_id, 'document'
    elif msg.audio: f_id, f_type = msg.audio.file_id, 'audio'
    elif msg.photo: f_id, f_type = msg.photo[-1].file_id, 'photo'
    elif msg.text and not msg.text.startswith('/'): f_id, f_type = msg.text, 'text'
    
    if f_id:
        context.user_data['multi_files'].append({'id': f_id, 'type': f_type})
        count = len(context.user_data['multi_files'])
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Done ✅", callback_data="done_media")]])
        await msg.reply_text(f"📦 কন্টেন্ট `{count}` যোগ হয়েছে। আরও কন্টেন্ট পাঠান অথবা 'Done' ক্লিক করুন।", reply_markup=keyboard, parse_mode='Markdown')
        return GET_MEDIA
    return GET_MEDIA

async def media_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("✍️ সব কন্টেন্ট পাওয়া গেছে। এখন একটি শিরোনাম (Title) দিন।")
    return GET_TITLE

async def get_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['tmp_title'] = update.message.text.strip()
    await update.message.reply_text("🔑 একটি ইউনিক কোড দিন (স্পেস ছাড়া)।")
    return GET_CUSTOM_CODE

async def get_custom_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    conn = get_db_connection()
    if not conn: return ConversationHandler.END
    try:
        with conn.cursor() as cur:
            files = context.user_data.get('multi_files')
            t = context.user_data.get('tmp_title')
            f_ids = "|".join([f['id'] for f in files])
            f_types = "|".join([f['type'] for f in files])
            cur.execute("INSERT INTO files (custom_code, title, file_type, file_id) VALUES (%s, %s, %s, %s)", (code, t, f_types, f_ids))
            conn.commit()
            bot_info = await context.bot.get_me()
            await update.message.reply_text(f"✅ **সফল!**\n\n🔗 লিঙ্ক:\n`https://t.me/{bot_info.username}?start={code}`", parse_mode='Markdown')
    except: await update.message.reply_text("❌ সমস্যা হয়েছে। কোডটি হয়তো আগে ব্যবহৃত।")
    finally: conn.close()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ প্রক্রিয়াটি বাতিল করা হয়েছে।")
    return ConversationHandler.END

# --- Flask Server ---
app = Flask('')
@app.route('/')
def home(): return "Bot is Online"
@app.route('/webapp-open/<int:user_id>')
def webapp_open(user_id):
    track_app_open(user_id)
    return {"status": "success"}

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def main():
    threading.Thread(target=run_flask).start()
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # অ্যাডমিন কন্টেন্ট জেনারেটর
    application.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.PHOTO | (filters.TEXT & ~filters.COMMAND), handle_admin_input)],
        states={
            GET_MEDIA: [
                MessageHandler(filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.PHOTO | (filters.TEXT & ~filters.COMMAND), add_to_media_list),
                CallbackQueryHandler(media_done_callback, pattern="^done_media$")
            ],
            GET_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_title)],
            GET_CUSTOM_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_code)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    
    # ব্রডকাস্ট কনভারসেশন
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_command)],
        states={
            GET_BROADCAST_MSG: [MessageHandler(filters.ALL & ~filters.COMMAND, send_broadcast)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("alllink", all_links))
    application.add_handler(CommandHandler("statics", statics_command))
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    
    application.run_polling()

if __name__ == '__main__':
    main()
