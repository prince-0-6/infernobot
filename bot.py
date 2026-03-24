import logging
import sqlite3
import os
from datetime import datetime
from dotenv import load_dotenv
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler
)

# ================= CONFIG =================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing in .env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB = "bot.db"

# ================= HEALTH CHECK SERVER =================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = json.dumps({"status": "ok", "message": "Bot is running"})
            self.wfile.write(response.encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        # Suppress HTTP server logs to avoid clutter
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    logger.info(f"✅ Health check server running on port {port}")
    server.serve_forever()

# ================= DATABASE =================
def db():
    return sqlite3.connect(DB)

def init_db():
    with db() as conn:
        c = conn.cursor()

        c.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            number INTEGER UNIQUE,
            link TEXT,
            active INTEGER DEFAULT 1
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            role TEXT
        )
        """)

        c.execute("""  
        CREATE TABLE IF NOT EXISTS broadcasts (
            message TEXT,
            date TEXT
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_seen TEXT
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)
        
        logger.info("✅ Database initialized")

# ================= HELPERS =================
def get_channels():
    with db() as conn:
        return conn.execute(
            "SELECT number, link FROM channels WHERE active=1 ORDER BY number ASC"
        ).fetchall()

def is_admin(uid: int) -> bool:
    with db() as conn:
        r = conn.execute("SELECT role FROM admins WHERE user_id=?", (uid,)).fetchone()
        return r is not None

def is_owner(uid: int) -> bool:
    with db() as conn:
        r = conn.execute("SELECT role FROM admins WHERE user_id=?", (uid,)).fetchone()
        return bool(r and r[0] == "owner")

def get_owner():
    with db() as conn:
        r = conn.execute("SELECT user_id FROM admins WHERE role='owner'").fetchone()
        return r[0] if r else None

def get_setting(key: str, default: str = "") -> str:
    with db() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

def set_setting(key: str, value: str):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

def add_owner(uid: int):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO admins VALUES (?,?)", (uid, "owner"))

def save_user(uid: int):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, first_seen) VALUES (?, ?)",
            (uid, datetime.now().isoformat())
        )

def build_channel_keyboard(channels, columns: int = 2):
    keyboard = []
    row = []
    for n, l in channels:
        row.append(InlineKeyboardButton(f"📢 CHANNEL {n}", url=l))
        if len(row) == columns:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return keyboard

# ================= FORCE JOIN =================
async def is_joined_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    channels = get_channels()

    if not channels:
        return False

    for _, link in channels:
        try:
            username = link.split("/")[-1].replace("@", "").strip()
            member = await context.bot.get_chat_member(f"@{username}", uid)
            if member.status in ["left", "kicked"]:
                return False
        except Exception as e:
            logger.warning(f"Join check failed for {link}: {e}")
            return False
    return True

async def force_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channels = get_channels()
    keyboard = build_channel_keyboard(channels, columns=2)
    keyboard.append([InlineKeyboardButton("✅ CHECK JOINED", callback_data="check")])
    markup = InlineKeyboardMarkup(keyboard)

    msg_text = get_setting("force_msg", "🚫 Please join all channels first to use this bot!")
    image_url = get_setting("force_image", "")

    if image_url:
        await update.message.reply_photo(
            photo=image_url,
            caption=msg_text,
            reply_markup=markup
        )
    else:
        await update.message.reply_text(msg_text, reply_markup=markup)

async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id

    if is_admin(uid):
        return True

    if not await is_joined_all(update, context):
        await force_join(update, context)
        return False

    return True

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    save_user(uid)

    # first user becomes owner
    if get_owner() is None:
        add_owner(uid)
        await update.message.reply_text("👑 You are now the OWNER of this bot!")
        return

    if not await guard(update, context):
        return

    if is_admin(uid):
        await update.message.reply_text(
            "⚙️ **Admin Panel Ready**\n\n"
            "Available commands:\n"
            "/add - Add channel\n"
            "/remove - Remove channel\n"
            "/update - Update channel\n"
            "/list - List channels\n"
            "/broadcast - Send broadcast\n"
            "/setmsg - Set force message\n"
            "/setimage - Set force image\n"
            "/stats - View stats",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("✅ Access Granted! You can now use the bot.")

async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if await is_joined_all(update, context):
        await q.edit_message_text("✅ **Verified!**\nUse /start to continue.", parse_mode='Markdown')
    else:
        await q.answer("❌ Please join all channels first!", show_alert=True)

# ================= CHANNEL MANAGEMENT =================
async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ You are not authorized!")
        return

    try:
        if len(context.args) < 2:
            raise ValueError
        n = int(context.args[0])
        l = context.args[1]

        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO channels VALUES (?,?,1)", (n, l))

        await update.message.reply_text(f"✅ Channel {n} added successfully!")
        logger.info(f"Channel {n} added by {update.effective_user.id}")

    except Exception:
        await update.message.reply_text("❌ Usage: /add <number> <channel_link>\nExample: /add 1 https://t.me/yourchannel")

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    try:
        n = int(context.args[0])
        with db() as conn:
            conn.execute("UPDATE channels SET active=0 WHERE number=?", (n,))
        await update.message.reply_text(f"✅ Channel {n} removed!")
        logger.info(f"Channel {n} removed by {update.effective_user.id}")
    except Exception:
        await update.message.reply_text("❌ Usage: /remove <number>")

async def update_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    try:
        if len(context.args) < 2:
            raise ValueError
        n = int(context.args[0])
        l = context.args[1]

        with db() as conn:
            conn.execute("UPDATE channels SET link=?, active=1 WHERE number=?", (l, n))

        await update.message.reply_text(f"✅ Channel {n} updated!")
    except Exception:
        await update.message.reply_text("❌ Usage: /update <number> <new_link>")

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return

    ch = get_channels()
    if not ch:
        await update.message.reply_text("📭 No channels configured yet.")
        return
    
    msg = "📺 **Active Channels:**\n\n"
    for n, l in ch:
        msg += f"`{n}` → {l}\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def channel_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return

    ch = get_channels()
    if not ch:
        await update.message.reply_text("📭 No channels available.")
        return

    kb = build_channel_keyboard(ch, columns=2)
    await update.message.reply_text(
        "📺 **Join our channels:**\n\nClick the buttons below to join!",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown'
    )

# ================= SETTINGS =================
async def set_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg = " ".join(context.args).strip()
    if not msg:
        await update.message.reply_text("❌ Usage: /setmsg <your message>")
        return
    set_setting("force_msg", msg)
    await update.message.reply_text(f"✅ Force join message set!\n\n{msg}")

async def set_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    url = " ".join(context.args).strip()
    if not url:
        await update.message.reply_text("❌ Usage: /setimage <image_url>")
        return
    set_setting("force_image", url)
    await update.message.reply_text("✅ Force join image set!")

# ================= BROADCAST =================
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    msg = " ".join(context.args).strip()
    if not msg:
        await update.message.reply_text("❌ Usage: /broadcast <your message>")
        return

    await update.message.reply_text("📢 Broadcasting started...")
    
    with db() as conn:
        user_rows = conn.execute("SELECT user_id FROM users").fetchall()

    success, fail = 0, 0
    for (uid,) in user_rows:
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
            success += 1
        except Exception as e:
            logger.warning(f"Broadcast failed to {uid}: {e}")
            fail += 1

    with db() as conn:
        conn.execute("INSERT INTO broadcasts VALUES (?,?)", (msg, datetime.now().isoformat()))

    await update.message.reply_text(f"✅ **Broadcast Complete**\n\n✅ Sent: {success}\n❌ Failed: {fail}", parse_mode='Markdown')

# ================= ADMIN MANAGEMENT =================
async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Only owner can add admins!")
        return

    try:
        uid = int(context.args[0])
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO admins VALUES (?,?)", (uid, "admin"))
        await update.message.reply_text(f"✅ Admin {uid} added!")
    except Exception:
        await update.message.reply_text("❌ Usage: /addadmin <user_id>")

async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return

    try:
        uid = int(context.args[0])
        if is_owner(uid):
            await update.message.reply_text("❌ Cannot remove owner!")
            return

        with db() as conn:
            conn.execute("DELETE FROM admins WHERE user_id=?", (uid,))
        await update.message.reply_text(f"✅ Admin {uid} removed!")
    except Exception:
        await update.message.reply_text("❌ Usage: /removeadmin <user_id>")

async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    with db() as conn:
        rows = conn.execute("SELECT user_id, role FROM admins").fetchall()

    if not rows:
        await update.message.reply_text("No admins found.")
        return

    msg = "👥 **Admins List:**\n\n"
    for uid, role in rows:
        msg += f"• `{uid}` → {role.upper()}\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

# ================= STATS =================
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return

    with db() as conn:
        c = conn.cursor()
        ch = c.execute("SELECT COUNT(*) FROM channels WHERE active=1").fetchone()[0]
        ad = c.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
        br = c.execute("SELECT COUNT(*) FROM broadcasts").fetchone()[0]
        us = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    await update.message.reply_text(
        f"📊 **Bot Statistics**\n\n"
        f"📺 Active Channels: {ch}\n"
        f"👮 Admins: {ad}\n"
        f"👥 Total Users: {us}\n"
        f"📢 Broadcasts Sent: {br}\n"
        f"🤖 Status: 🟢 Online",
        parse_mode='Markdown'
    )

# ================= MAIN =================
def main():
    # Initialize database
    init_db()
    
    # Start health check server in background thread
    health_thread = Thread(target=run_health_server, daemon=True)
    health_thread.start()
    
    # Build application
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_channel))
    app.add_handler(CommandHandler("remove", remove_channel))
    app.add_handler(CommandHandler("update", update_channel))
    app.add_handler(CommandHandler("list", list_channels))
    app.add_handler(CommandHandler("channels", channel_buttons))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("addadmin", add_admin))
    app.add_handler(CommandHandler("removeadmin", remove_admin))
    app.add_handler(CommandHandler("admins", list_admins))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("setmsg", set_message))
    app.add_handler(CommandHandler("setimage", set_image))
    
    app.add_handler(CallbackQueryHandler(check_join, pattern="check"))
    
    logger.info("🤖 Bot started! Running with polling...")
    logger.info(f"Health check available at: http://localhost:{os.environ.get('PORT', 10000)}/health")
    
    # Start polling
    app.run_polling()

if __name__ == "__main__":
    main()
