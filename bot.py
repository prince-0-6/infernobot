import logging
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

from pymongo import MongoClient

# ================= CONFIG =================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_SECRET = os.getenv("OWNER_SECRET")
MONGO_URI = os.getenv("MONGO_URI")  # e.g. mongodb+srv://user:pass@cluster.mongodb.net/botdb
PORT = int(os.getenv("PORT", 10000))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing in .env")
if not OWNER_SECRET:
    raise ValueError("OWNER_SECRET missing in .env")
if not MONGO_URI:
    raise ValueError("MONGO_URI missing in .env")

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ================= MONGODB SETUP =================
mongo_client = MongoClient(MONGO_URI)
mongo_db = mongo_client.get_default_database()  # uses DB name from URI

channels_col   = mongo_db["channels"]
admins_col     = mongo_db["admins"]
broadcasts_col = mongo_db["broadcasts"]
users_col      = mongo_db["users"]
settings_col   = mongo_db["settings"]

def init_db():
    """Create indexes for fast lookups."""
    channels_col.create_index("number", unique=True)
    admins_col.create_index("user_id", unique=True)
    users_col.create_index("user_id", unique=True)
    settings_col.create_index("key", unique=True)

# ================= WEB SERVER =================
HTML_PAGE = """<!DOCTYPE html>
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
  <h1>&#x1F916; Telegram Bot</h1>
  <p class="sub">Powered by python-telegram-bot</p>
  <div class="badge"><span class="dot"></span>ONLINE &amp; ACTIVE</div>
  <p class="info">
    Bot is running smoothly<br>
    All systems operational<br><br>
    &copy; 2026 &mdash; All rights reserved @aerivue
  </p>
</div>
</body>
</html>"""

class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode("utf-8"))

    def log_message(self, *args):
        pass

def run_web_server():
    HTTPServer(("0.0.0.0", PORT), WebHandler).serve_forever()

# ================= HELPERS =================
def get_channels():
    """Return list of (number, link) tuples for active channels."""
    docs = channels_col.find({"active": True}, {"_id": 0, "number": 1, "link": 1}).sort("number", 1)
    return [(d["number"], d["link"]) for d in docs]

def is_admin(uid: int) -> bool:
    return admins_col.find_one({"user_id": uid}) is not None

def is_owner(uid: int) -> bool:
    doc = admins_col.find_one({"user_id": uid})
    return bool(doc and doc.get("role") == "owner")

def get_owner():
    doc = admins_col.find_one({"role": "owner"})
    return doc["user_id"] if doc else None

def get_setting(key: str, default: str = "") -> str:
    doc = settings_col.find_one({"key": key})
    return doc["value"] if doc else default

def set_setting(key: str, value: str):
    settings_col.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)

def add_owner(uid: int):
    admins_col.update_one({"user_id": uid}, {"$set": {"user_id": uid, "role": "owner"}}, upsert=True)

def save_user(uid: int):
    users_col.update_one(
        {"user_id": uid},
        {"$setOnInsert": {"user_id": uid, "first_seen": datetime.now().isoformat()}},
        upsert=True
    )

def build_channel_keyboard(channels, columns: int = 2):
    keyboard, row = [], []
    for n, l in channels:
        row.append(InlineKeyboardButton(f"𝐂𝐇𝐀𝐍𝐍𝐄𝐋 {n}", url=l))
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
    keyboard.append([InlineKeyboardButton(
        "𝐂ʜᴇᴄᴋ 𝐉ᴏɪɴᴇᴅ ✅",
        url="https://t.me/+U7N9wRhh6EtmOWM1")])
    markup = InlineKeyboardMarkup(keyboard)
    msg_text = get_setting("force_msg", "Join all channels first!")
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
            await update.message.reply_text("You are already the OWNER!")
        else:
            await update.message.reply_text("Owner is already set.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /owner <secret_password>")
        return

    secret = context.args[0].strip()
    if secret != OWNER_SECRET:
        await update.message.reply_text("Wrong secret! Access denied.")
        return

    add_owner(uid)
    name = update.effective_user.first_name or "Owner"
    await update.message.reply_text(
        f"Welcome, *{name}*!\n\n"
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
            "Bot not configured yet.\n\n"
            "Owner must run: /owner <secret_password>"
        )
        return

    if not await guard(update, context):
        return

    if is_owner(uid):
        await update.message.reply_text(
            "*Owner Panel*\n\n"
            "/add - Add channel\n"
            "/remove - Remove channel\n"
            "/update - Update channel link\n"
            "/list - List channels\n"
            "/broadcast - Broadcast to all users\n"
            "/setmsg - Set force join message\n"
            "/setimage - Set force join image\n"
            "/addadmin - Add admin\n"
            "/removeadmin - Remove admin\n"
            "/admins - List admins\n"
            "/stats - Bot statistics",
            parse_mode="Markdown"
        )
    elif is_admin(uid):
        await update.message.reply_text(
            "*Admin Panel*\n\n"
            "/add - Add channel\n"
            "/remove - Remove channel\n"
            "/update - Update channel link\n"
            "/list - List channels\n"
            "/broadcast - Broadcast to all users\n"
            "/setmsg - Set force join message\n"
            "/setimage - Set force join image\n"
            "/stats - Bot statistics",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("Access Granted!")

# ================= CALLBACK =================
async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if await is_joined_all(update, context):
        await q.edit_message_text("Verified! Use /start to continue.")
    else:
        await q.answer("Join all channels first!", show_alert=True)

# ================= CHANNEL MANAGEMENT =================
async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        n = int(context.args[0])
        l = context.args[1]
        channels_col.update_one(
            {"number": n},
            {"$set": {"number": n, "link": l, "active": True}},
            upsert=True
        )
        await update.message.reply_text(f"Channel {n} added!")
    except Exception:
        await update.message.reply_text("Usage: /add 1 https://t.me/yourchannel")

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        n = int(context.args[0])
        channels_col.update_one({"number": n}, {"$set": {"active": False}})
        await update.message.reply_text(f"Channel {n} removed!")
    except Exception:
        await update.message.reply_text("Usage: /remove 1")

async def update_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        n = int(context.args[0])
        l = context.args[1]
        channels_col.update_one({"number": n}, {"$set": {"link": l, "active": True}})
        await update.message.reply_text(f"Channel {n} updated!")
    except Exception:
        await update.message.reply_text("Usage: /update 1 https://t.me/newlink")

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    ch = get_channels()
    if not ch:
        await update.message.reply_text("No channels added yet.")
        return
    msg = "*Active Channels:*\n\n"
    for n, l in ch:
        msg += f"`{n}` - {l}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def channel_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    ch = get_channels()
    if not ch:
        await update.message.reply_text("No channels available.")
        return
    kb = build_channel_keyboard(ch, columns=2)
    await update.message.reply_text("Join our channels:", reply_markup=InlineKeyboardMarkup(kb))

# ================= SETTINGS =================
async def set_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg = " ".join(context.args).strip()
    if not msg:
        await update.message.reply_text("Usage: /setmsg Your message here")
        return
    set_setting("force_msg", msg)
    await update.message.reply_text(f"Force message updated!\n\n{msg}")

async def set_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    url = " ".join(context.args).strip()
    if not url:
        await update.message.reply_text("Usage: /setimage https://...")
        return
    set_setting("force_image", url)
    await update.message.reply_text("Force image updated!")

# ================= BROADCAST =================
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg = " ".join(context.args).strip()
    if not msg:
        await update.message.reply_text("Usage: /broadcast your message")
        return

    # Fetch all saved user IDs from MongoDB
    user_docs = users_col.find({}, {"user_id": 1, "_id": 0})
    success, fail = 0, 0
    for doc in user_docs:
        try:
            await context.bot.send_message(chat_id=doc["user_id"], text=msg)
            success += 1
        except Exception:
            fail += 1

    broadcasts_col.insert_one({"message": msg, "date": datetime.now().isoformat()})
    await update.message.reply_text(f"Broadcast done!\n\nSent: {success}\nFailed: {fail}")

# ================= ADMIN MANAGEMENT =================
async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    try:
        uid = int(context.args[0])
        admins_col.update_one(
            {"user_id": uid},
            {"$set": {"user_id": uid, "role": "admin"}},
            upsert=True
        )
        await update.message.reply_text(f"Admin {uid} added!")
    except Exception:
        await update.message.reply_text("Usage: /addadmin <user_id>")

async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    try:
        uid = int(context.args[0])
        if is_owner(uid):
            await update.message.reply_text("Cannot remove owner!")
            return
        admins_col.delete_one({"user_id": uid})
        await update.message.reply_text(f"Admin {uid} removed!")
    except Exception:
        await update.message.reply_text("Usage: /removeadmin <user_id>")

async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    docs = admins_col.find({}, {"_id": 0, "user_id": 1, "role": 1})
    rows = [(d["user_id"], d["role"]) for d in docs]
    if not rows:
        await update.message.reply_text("No admins found.")
        return
    msg = "*Admins:*\n\n"
    for uid, role in rows:
        msg += f"- `{uid}` - {role.upper()}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

# ================= STATS =================
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    ch = channels_col.count_documents({"active": True})
    ad = admins_col.count_documents({})
    br = broadcasts_col.count_documents({})
    us = users_col.count_documents({})
    await update.message.reply_text(
        f"*Bot Stats*\n\n"
        f"Channels: {ch}\n"
        f"Admins: {ad}\n"
        f"Users: {us}\n"
        f"Broadcasts: {br}\n"
        f"Status: Online",
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
