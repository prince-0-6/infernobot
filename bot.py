import logging
import sqlite3
import os
from datetime import datetime
from dotenv import load_dotenv
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler
)

# ================= CONFIG =================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_SECRET = os.getenv("OWNER_SECRET")
PORT = int(os.getenv("PORT", 10000))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing in .env")
if not OWNER_SECRET:
    raise ValueError("OWNER_SECRET missing in .env")

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

DB = "bot.db"

# ================= WEB SERVER =================
class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        html = b"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bot Status</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    min-height: 100vh;
    background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
    display: flex; align-items: center; justify-content: center;
    font-family: 'Segoe UI', sans-serif; color: white;
  }
  .card {
    background: rgba(255,255,255,0.07);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 24px;
    padding: 50px 60px;
    text-align: center;
    max-width: 480px; width: 90%;
    box-shadow: 0 30px 60px rgba(0,0,0,0.4);
  }
  .dot {
    width: 16px; height: 16px;
    background: #00ff88; border-radius: 50%;
    display: inline-block; margin-right: 8px;
    vertical-align: middle;
    animation: pulse 1.5s infinite;
  }
  @keyframes pulse {
    0%,100% { box-shadow: 0 0 0 0 rgba(0,255,136,0.5); }
    50% { box-shadow: 0 0 0 10px rgba(0,255,136,0); }
  }
  h1 { font-size: 2.2rem; margin-bottom: 8px; letter-spacing: 1px; }
  .sub { color: rgba(255,255,255,0.45); font-size: 0.9rem; margin-bottom: 36px; }
  .badge {
    display: inline-block;
    background: rgba(0,255,136,0.12);
    border: 1px solid rgba(0,255,136,0.35);
    color: #00ff88;
    border-radius: 50px;
    padding: 10px 28px;
    font-size: 1rem; font-weight: 600;
    letter-spacing: 1px;
    margin-bottom: 30px;
  }
  .info { color: rgba(255,255,255,0.35); font-size: 0.82rem; line-height: 1.8; }
</style>
</head>
<body>
<div class="card">
  <h1>🤖 Telegram Bot</h1>
  <p class="sub">Powered by python-telegram-bot</p>
  <div class="badge"><span class="dot"></span>ONLINE &amp; ACTIVE</div>
  <p class="info">
    Bot is running smoothly<br>
    All systems operational<br><br>
    &copy; 2025 &mdash; All rights reserved
  </p>
</div>
</body>
</html>"""
        self.wfile.write(html)

    def log_message(self, *args):
        pass

def run_web_server():
    HTTPServer(("0.0.0.0", PORT), WebHandler).serve_forever()

# ================= DATABASE =================
def db():
    return sqlite3.connect(DB)

def init_db():
    with db() as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS channels (
            number INTEGER UNIQUE, link TEXT, active INTEGER DEFAULT 1
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY, role TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS broadcasts (
            message TEXT, date TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, first_seen TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )""")

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
            "INSERT OR IGNORE INTO users (user_id, first_seen) VALUES (?,?)",
            (uid, datetime.now().isoformat())
        )

def build_channel_keyboard(channels, columns: int = 2):
    keyboard, row = [], []
    for n, l in channels:
        row.append(InlineKeyboardButton(f"CHANNEL {n}", url=l))
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
        except Exception:
            return False
    return True

async def force_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channels = get_channels()
    keyboard = build_channel_keyboard(channels, columns=2)
    keyboard.append([InlineKeyboardButton("✅ CHECK JOINED", callback_data="check")])
    markup = InlineKeyboardMarkup(keyboard)
    msg_text = get_setting("force_msg", "🚫 Join all channels first!")
    image_url = get_setting("force_image", "")
    if image_url:
        await update.message.reply_photo(photo=image_url, caption=msg_text, reply_markup=markup)
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

# ================= /owner =================
async def owner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if get_owner() is not None:
        if is_owner(uid):
            await update.message.reply_text("👑 You are already the OWNER!")
        else:
            await update.message.reply_text("❌ Owner is already set.")
        return

    if not context.args:
        await update.message.reply_text("🔐 Usage: /owner <secret_password>")
        return

    secret = context.args[0].strip()
    if secret != OWNER_SECRET:
        await update.message.reply_text("❌ Wrong secret! Access denied.")
        return

    add_owner(uid)
    name = update.effective_user.first_name or "Owner"
    await update.message.reply_text(
        f"👑 Welcome, *{name}*!\n\n"
        f"You are now the OWNER of this bot.\n"
        f"Use /start to see all commands.",
        parse_mode="Markdown"
    )

# ================= /start =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    save_user(uid)

    if get_owner() is None:
        await update.message.reply_text(
            "⚙️ Bot not configured yet.\n\n"
            "Owner must run: /owner <secret_password>"
        )
        return

    if not await guard(update, context):
        return

    if is_owner(uid):
        await update.message.reply_text(
            "👑 *Owner Panel*\n\n"
            "/add — Add channel\n"
            "/remove — Remove channel\n"
            "/update — Update channel link\n"
            "/list — List channels\n"
            "/broadcast — Broadcast to all users\n"
            "/setmsg — Set force join message\n"
            "/setimage — Set force join image\n"
            "/addadmin — Add admin\n"
            "/removeadmin — Remove admin\n"
            "/admins — List admins\n"
            "/stats — Bot statistics",
            parse_mode="Markdown"
        )
    elif is_admin(uid):
        await update.message.reply_text(
            "⚙️ *Admin Panel*\n\n"
            "/add — Add channel\n"
            "/remove — Remove channel\n"
            "/update — Update channel link\n"
            "/list — List channels\n"
            "/broadcast — Broadcast to all users\n"
            "/setmsg — Set force join message\n"
            "/setimage — Set force join image\n"
            "/stats — Bot statistics",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("✅ Access Granted!")

# ================= CALLBACK =================
async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if await is_joined_all(update, context):
        await q.edit_message_text("✅ Verified! Use /start to continue.")
    else:
        await q.answer("❌ Join all channels first!", show_alert=True)

# ================= CHANNEL MANAGEMENT =================
async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        n = int(context.args[0])
        l = context.args[1]
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO channels VALUES (?,?,1)", (n, l))
        await update.message.reply_text(f"✅ Channel {n} added!")
    except Exception:
        await update.message.reply_text("❌ Usage: /add 1 https://t.me/yourchannel")

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        n = int(context.args[0])
        with db() as conn:
            conn.execute("UPDATE channels SET active=0 WHERE number=?", (n,))
        await update.message.reply_text(f"✅ Channel {n} removed!")
    except Exception:
        await update.message.reply_text("❌ Usage: /remove 1")

async def update_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        n = int(context.args[0])
        l = context.args[1]
        with db() as conn:
            conn.execute("UPDATE channels SET link=?, active=1 WHERE number=?", (l, n))
        await update.message.reply_text(f"✅ Channel {n} updated!")
    except Exception:
        await update.message.reply_text("❌ Usage: /update 1 https://t.me/newlink")

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    ch = get_channels()
    if not ch:
        await update.message.reply_text("📭 No channels added yet.")
        return
    msg = "📺 *Active Channels:*\n\n"
    for n, l in ch:
        msg += f"`{n}` → {l}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def channel_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    ch = get_channels()
    if not ch:
        await update.message.reply_text("📭 No channels available.")
        return
    kb = build_channel_keyboard(ch, columns=2)
    await update.message.reply_text("📺 Join our channels:", reply_markup=InlineKeyboardMarkup(kb))

# ================= SETTINGS =================
async def set_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg = " ".join(context.args).strip()
    if not msg:
        await update.message.reply_text("❌ Usage: /setmsg Your message here")
        return
    set_setting("force_msg", msg)
    await update.message.reply_text(f"✅ Force message updated!\n\n{msg}")

async def set_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    url = " ".join(context.args).strip()
    if not url:
        await update.message.reply_text("❌ Usage: /setimage https://...")
        return
    set_setting("force_image", url)
    await update.message.reply_text("✅ Force image updated!")

# ================= BROADCAST =================
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg = " ".join(context.args).strip()
    if not msg:
        await update.message.reply_text("❌ Usage: /broadcast your message")
        return
    with db() as conn:
        user_rows = conn.execute("SELECT user_id FROM users").fetchall()
    success, fail = 0, 0
    for (uid,) in user_rows:
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
            success += 1
        except Exception:
            fail += 1
    with db() as conn:
        conn.execute("INSERT INTO broadcasts VALUES (?,?)", (msg, datetime.now().isoformat()))
    await update.message.reply_text(f"📢 Broadcast done!\n\n✅ Sent: {success}\n❌ Failed: {fail}")

# ================= ADMIN MANAGEMENT =================
async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
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
    msg = "👥 *Admins:*\n\n"
    for uid, role in rows:
        msg += f"• `{uid}` → {role.upper()}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

# ================= STATS =================
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    with db() as conn:
        c = conn.cursor()
        ch = c.execute("SELECT COUNT(*) FROM channels WHERE active=1").fetchone()[0]
        ad = c.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
        br = c.execute("SELECT COUNT(*) FROM broadcasts").fetchone()[0]
        us = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    await update.message.reply_text(
        f"📊 *Bot Stats*\n\n"
        f"📺 Channels: {ch}\n"
        f"👮 Admins: {ad}\n"
        f"👥 Users: {us}\n"
        f"📢 Broadcasts: {br}\n"
        f"🟢 Status: Online",
        parse_mode="Markdown"
    )

# ================= MAIN =================
def main():
    init_db()
    Thread(target=run_web_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("owner", owner_cmd))
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

    app.run_polling()

if __name__ == "__main__":
    main()
