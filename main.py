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
        logger.error("DATABASE_URL variable missing in environment!")
        return None
    try:
        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url, sslmode='require', connect_timeout=10)
        return conn
    except Exception as e:
        logger.error(f"Critical Database Connection Error: {e}")
        return None

# ------------------- কনফিগারেশন -------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", 0))

# কথোপকথনের ধাপ (States)
GET_TITLE, GET_CUSTOM_CODE, GET_BROADCAST_MSG = range(3)

def init_db():
    """প্রয়োজনীয় টেবিল এবং কলাম তৈরি বা অটো-আপডেট করে"""
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                # ১. প্রথমে users টেবিল নিশ্চিত করা
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT PRIMARY KEY,
                        username TEXT,
                        full_name TEXT,
                        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # ২. কলামগুলো না থাকলে যোগ করা
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT")
                
                # ৩. মিনি অ্যাপ ওপেন ট্র্যাকিং এর জন্য টেবিল তৈরি (যদি না থাকে)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS app_logs (
                        user_id BIGINT,
                        last_open TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.commit()
                logger.info("Database initialized successfully.")
        except Exception as e:
            logger.error(f"DB Init Error: {e}")
        finally:
            conn.close()

def save_user(user_id, username, full_name):
    """ইউজার আইডি, ইউজারনেম এবং টেলিগ্রাম নাম ডাটাবেসে সেভ করে"""
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
        except Exception as e:
            logger.error(f"Error saving user: {e}")
        finally:
            conn.close()

def track_app_open(user_id):
    """মিনি অ্যাপ ওপেন ট্র্যাকিং (২৪ ঘণ্টা ইউনিক লজিক)"""
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(last_open) FROM app_logs WHERE user_id = %s", (user_id,))
                res = cur.fetchone()
                
                now = datetime.now()
                should_insert = False
                
                if res[0] is None:
                    should_insert = True
                else:
                    last_open_time = res[0]
                    if now - last_open_time >= timedelta(hours=24):
                        should_insert = True
                
                if should_insert:
                    cur.execute("INSERT INTO app_logs (user_id, last_open) VALUES (%s, %s)", (user_id, now))
                    conn.commit()
        except Exception as e:
            logger.error(f"Error tracking app open: {e}")
        finally:
            conn.close()

async def post_init(application: Application):
    """বট মেনু কমান্ড সেটআপ"""
    init_db()
    user_commands = [BotCommand("start", "বট শুরু করুন")]
    await application.bot.set_my_commands(user_commands)
    
    if ADMIN_USER_ID:
        admin_commands = [
            BotCommand("start", "বট শুরু করুন"),
            BotCommand("alllink", "সব ফাইলের তালিকা"),
            BotCommand("broadcast", "সবাইকে মেসেজ পাঠান"),
            BotCommand("statics", "বটের পরিসংখ্যান"),
            BotCommand("cancel", "বর্তমান কাজ বাতিল")
        ]
        try:
            await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_USER_ID))
        except Exception as e:
            logger.error(f"Failed to set admin commands: {e}")

# --- বট ফাংশনসমূহ ---

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
                f_type, f_id, title = res
                await context.bot.send_message(chat_id=user.id, text=f"*{title}*", parse_mode='Markdown')
                if f_type == 'video': await context.bot.send_video(chat_id=user.id, video=f_id, protect_content=True)
                elif f_type == 'document': await context.bot.send_document(chat_id=user.id, document=f_id, protect_content=True)
                elif f_type == 'audio': await context.bot.send_audio(chat_id=user.id, audio=f_id, protect_content=True)
                elif f_type == 'photo': await context.bot.send_photo(chat_id=user.id, photo=f_id, protect_content=True)
        finally:
            conn.close()
    else:
        await update.message.reply_text(f"স্বাগতম {user.first_name}!")

async def statics_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """বটের বিস্তারিত পরিসংখ্যান দেখায়"""
    if update.effective_user.id != ADMIN_USER_ID:
        logger.warning(f"Unauthorized access attempt to /statics by {update.effective_user.id}")
        return
    
    conn = get_db_connection()
    if not conn:
        await update.message.reply_text("❌ ডাটাবেস কানেকশন এরর!")
        return
        
    try:
        with conn.cursor() as cur:
            # মোট ইউজার
            cur.execute("SELECT COUNT(*) FROM users")
            total_users = cur.fetchone()[0]
            
            # আজকের নতুন ইউজার
            cur.execute("SELECT COUNT(*) FROM users WHERE joined_at >= CURRENT_DATE")
            today_users = cur.fetchone()[0]
            
            # মোট অ্যাপ ওপেন
            cur.execute("SELECT COUNT(*) FROM app_logs")
            total_app_opens = cur.fetchone()[0]
            
            # গত ২৪ ঘণ্টায় ইউনিক অ্যাপ ওপেন
            cur.execute("SELECT COUNT(*) FROM app_logs WHERE last_open >= (NOW() - INTERVAL '24 HOURS')")
            today_app_opens = cur.fetchone()[0]
            
            stats_msg = (
                "📊 **বট পরিসংখ্যান**\n\n"
                f"👥 **ইউজার পরিসংখ্যান:**\n"
                f"  • আজকে নতুন: {today_users}\n"
                f"  • মোট ইউজার: {total_users}\n\n"
                f"📱 **মিনি অ্যাপ পরিসংখ্যান (ইউনিক):**\n"
                f"  • গত ২৪ ঘণ্টায়: {today_app_opens}\n"
                f"  • মোট ওপেন (লাইফটাইম): {total_app_opens}\n\n"
                f"📅 তারিখ: {datetime.now().strftime('%d %B, %Y')}"
            )
            await update.message.reply_text(stats_msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Statics Command Error: {e}")
        await update.message.reply_text(f"❌ একটি সমস্যা হয়েছে: {str(e)}")
    finally:
        conn.close()

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_USER_ID: return ConversationHandler.END
    await update.message.reply_text("ব্রডকাস্ট মেসেজটি দিন।")
    return GET_BROADCAST_MSG

async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin_msg = update.message
    conn = get_db_connection()
    if not conn: return ConversationHandler.END
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users")
            users = cur.fetchall()
        for (u_id,) in users:
            try: await context.bot.copy_message(chat_id=u_id, from_chat_id=admin_msg.chat_id, message_id=admin_msg.message_id, protect_content=True)
            except: continue
        await update.message.reply_text("✅ ব্রডকাস্ট সম্পন্ন।")
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
            await update.message.reply_text('📂 ফাইলের তালিকা:', reply_markup=InlineKeyboardMarkup(keyboard))
    finally: conn.close()

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    bot_info = await context.bot.get_me()
    await query.message.reply_text(f"লিঙ্ক:\n`https://t.me/{bot_info.username}?start={query.data}`", parse_mode='Markdown')

async def handle_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_USER_ID: return ConversationHandler.END
    msg = update.message
    f_id, f_type = None, None
    if msg.video: f_id, f_type = msg.video.file_id, 'video'
    elif msg.document: f_id, f_type = msg.document.file_id, 'document'
    elif msg.audio: f_id, f_type = msg.audio.file_id, 'audio'
    elif msg.photo: f_id, f_type = msg.photo[-1].file_id, 'photo'
    if f_id:
        context.user_data['tmp_file'] = {'id': f_id, 'type': f_type}
        await msg.reply_text("✍️ শিরোনাম (Title) দিন।")
        return GET_TITLE
    return ConversationHandler.END

async def get_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['tmp_title'] = update.message.text.strip()
    await update.message.reply_text("🔑 ইউনিক কোড দিন (স্পেস ছাড়া)।")
    return GET_CUSTOM_CODE

async def get_custom_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    conn = get_db_connection()
    if not conn: return ConversationHandler.END
    try:
        with conn.cursor() as cur:
            f = context.user_data.get('tmp_file')
            t = context.user_data.get('tmp_title')
            cur.execute("INSERT INTO files (custom_code, title, file_type, file_id) VALUES (%s, %s, %s, %s)", (code, t, f['type'], f['id']))
            conn.commit()
            bot_info = await context.bot.get_me()
            await update.message.reply_text(f"✅ সফল! লিঙ্ক:\n`https://t.me/{bot_info.username}?start={code}`", parse_mode='Markdown')
    finally: conn.close()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ বাতিল করা হয়েছে।")
    return ConversationHandler.END

# --- Flask Server ---
app = Flask('')
@app.route('/')
def home(): return "Bot is Online"

@app.route('/webapp-open/<int:user_id>')
def webapp_open(user_id):
    """২৪ ঘণ্টা পর পর মিনি অ্যাপ ওপেন কাউন্ট হবে"""
    track_app_open(user_id)
    return {"status": "success", "user_id": user_id}

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def main():
    threading.Thread(target=run_flask).start()
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    application.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.PHOTO, handle_media_upload)],
        states={GET_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_title)], GET_CUSTOM_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_code)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_command)],
        states={GET_BROADCAST_MSG: [MessageHandler(filters.ALL & ~filters.COMMAND, send_broadcast)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("alllink", all_links))
    application.add_handler(CommandHandler("statics", statics_command))
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    
    application.run_polling()

if __name__ == '__main__':
    main()
