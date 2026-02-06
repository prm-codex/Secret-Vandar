import logging
import os
import threading
import asyncio
import psycopg2
from flask import Flask

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
        
        # পোলার কানেকশনের জন্য sslmode require থাকা জরুরি
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

def save_user(user_id, username):
    """ইউজার আইডি এবং ইউজারনেম ডাটাবেসে সেভ করে"""
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, username) VALUES (%s, %s) "
                    "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username",
                    (user_id, username)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Error saving user to DB: {e}")
        finally:
            conn.close()

async def post_init(application: Application):
    """বট মেনু কমান্ড সেটআপ"""
    user_commands = [BotCommand("start", "বট শুরু করুন")]
    await application.bot.set_my_commands(user_commands)
    
    if ADMIN_USER_ID:
        admin_commands = [
            BotCommand("start", "বট শুরু করুন"),
            BotCommand("alllink", "সব ফাইলের তালিকা"),
            BotCommand("broadcast", "সবাইকে মেসেজ পাঠান"),
            BotCommand("cancel", "বর্তমান কাজ বাতিল")
        ]
        try:
            await application.bot.set_my_commands(
                admin_commands, 
                scope=BotCommandScopeChat(chat_id=ADMIN_USER_ID)
            )
        except Exception as e:
            logger.error(f"Failed to set admin commands: {e}")

# --- বট ফাংশনসমূহ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    save_user(user.id, user.username)
    
    if context.args:
        file_code = context.args[0]
        conn = get_db_connection()
        if not conn:
            await update.message.reply_text("ডেটাবেস কানেকশন এরর!")
            return
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT file_type, file_id, title FROM files WHERE custom_code = %s", (file_code,))
                res = cur.fetchone()
            if res:
                f_type, f_id, title = res
                await context.bot.send_message(chat_id=user.id, text=f"*{title}*", parse_mode='Markdown')
                # protect_content=True যাতে ডাউনলোড/ফরওয়ার্ড বন্ধ থাকে
                if f_type == 'video': await context.bot.send_video(chat_id=user.id, video=f_id, protect_content=True)
                elif f_type == 'document': await context.bot.send_document(chat_id=user.id, document=f_id, protect_content=True)
                elif f_type == 'audio': await context.bot.send_audio(chat_id=user.id, audio=f_id, protect_content=True)
                elif f_type == 'photo': await context.bot.send_photo(chat_id=user.id, photo=f_id, protect_content=True)
            else:
                await update.message.reply_text("দুঃখিত, এই লিঙ্কটি সঠিক নয়।")
        finally:
            conn.close()
    else:
        await update.message.reply_text(f"স্বাগতম {user.first_name} এই বটে আপনি নিয়মিত নতুন লিংকের আপডেট পাবেন।")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_USER_ID: return ConversationHandler.END
    await update.message.reply_text("ব্রডকাস্ট মেসেজটি দিন। বাতিল করতে /cancel লিখুন।")
    return GET_BROADCAST_MSG

async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin_msg = update.message
    conn = get_db_connection()
    if not conn: return ConversationHandler.END
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users")
            users = cur.fetchall()
        
        await update.message.reply_text(f"ব্রডকাস্টিং শুরু... ইউজার সংখ্যা: {len(users)}")
        success = 0
        for (u_id,) in users:
            try:
                await context.bot.copy_message(chat_id=u_id, from_chat_id=admin_msg.chat_id, message_id=admin_msg.message_id, protect_content=True)
                success += 1
                await asyncio.sleep(0.05)
            except: continue
        await update.message.reply_text(f"ব্রডকাস্ট সম্পন্ন। সফল: {success} জন।")
    finally:
        conn.close()
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
            await update.message.reply_text('ফাইলের তালিকা:', reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text("কোনো ফাইল সেভ করা নেই।")
    finally:
        conn.close()

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
        await msg.reply_text("ফাইল পাওয়া গেছে। এখন একটি শিরোনাম (Title) দিন।")
        return GET_TITLE
    return ConversationHandler.END

async def get_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['tmp_title'] = update.message.text.strip()
    await update.message.reply_text("শিরোনাম সেট হয়েছে। এখন একটি ইউনিক কোড দিন (স্পেস ছাড়া)।")
    return GET_CUSTOM_CODE

async def get_custom_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    if ' ' in code:
        await update.message.reply_text("স্পেস ছাড়া কোড দিন।")
        return GET_CUSTOM_CODE
        
    conn = get_db_connection()
    if not conn: return ConversationHandler.END
    try:
        with conn.cursor() as cur:
            f = context.user_data.get('tmp_file')
            t = context.user_data.get('tmp_title')
            cur.execute("INSERT INTO files (custom_code, title, file_type, file_id) VALUES (%s, %s, %s, %s)", (code, t, f['type'], f['id']))
            conn.commit()
            bot_info = await context.bot.get_me()
            await update.message.reply_text(f"সফল! লিঙ্ক তৈরি হয়েছে:\n`https://t.me/{bot_info.username}?start={code}`", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"DB Save Error: {e}")
        await update.message.reply_text("সম্ভবত কোডটি ইতিমধ্যে ব্যবহৃত।")
    finally:
        conn.close()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("প্রক্রিয়াটি বাতিল করা হয়েছে।")
    return ConversationHandler.END

# --- Flask Server (Koyeb এর জন্য পোর্ট ডাইনামিক করা হয়েছে) ---
app = Flask('')
@app.route('/')
def home(): return "Bot is Online"

def run_flask():
    # Koyeb অটোমেটিক PORT এনভায়রনমেন্ট ভেরিয়েবল প্রদান করে
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def main():
    threading.Thread(target=run_flask).start()
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    application.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.PHOTO, handle_media_upload)],
        states={
            GET_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_title)],
            GET_CUSTOM_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_code)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_command)],
        states={GET_BROADCAST_MSG: [MessageHandler(filters.ALL & ~filters.COMMAND, send_broadcast)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("alllink", all_links))
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    
    logger.info("Starting Polling for Koyeb...")
    application.run_polling()

if __name__ == '__main__':

    main()
