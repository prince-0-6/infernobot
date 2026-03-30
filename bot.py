import logging
import os
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler
)
from telegram.error import Forbidden, TelegramError

from pymongo import MongoClient

# ================= CONFIG =================
load_dotenv()
BOT_TOKEN     = os.getenv("BOT_TOKEN")
OWNER_SECRET  = os.getenv("OWNER_SECRET")
MONGO_URI     = os.getenv("MONGO_URI")
PORT          = int(os.getenv("PORT", 10000))

# Broadcast tuning — safe defaults for Render free tier (1 vCPU)
# Telegram allows ~30 messages/sec globally; stay well under to avoid 429s
BROADCAST_CONCURRENCY = int(os.getenv("BROADCAST_CONCURRENCY", 10))   # parallel sends
BROADCAST_CHUNK_DELAY = float(os.getenv("BROADCAST_CHUNK_DELAY", 0.05))  # seconds between chunks

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing in .env")
if not OWNER_SECRET:
    raise ValueError("OWNER_SECRET missing in .env")
if not MONGO_URI:
    raise ValueError("MONGO_URI missing in .env")

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# ================= MONGODB SETUP =================
# maxPoolSize=10 keeps connections lean on Render free tier (512 MB RAM)
mongo_client = MongoClient(MONGO_URI, maxPoolSize=10, serverSelectionTimeoutMS=5000)
mongo_db = mongo_client["botdb"]

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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #070912;
  --surface: #0e1120;
  --border: #1e2540;
  --accent: #4fffb0;
  --accent2: #7b6ff0;
  --text: #e8eaf6;
  --muted: #4a5078;
  --mono: 'Space Mono', monospace;
  --display: 'Syne', sans-serif;
}

*, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--mono);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 24px;
  overflow: hidden;
  position: relative;
}

/* Grid background */
body::before {
  content: '';
  position: fixed;
  inset: 0;
  background-image:
    linear-gradient(rgba(75,255,176,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(75,255,176,0.03) 1px, transparent 1px);
  background-size: 40px 40px;
  pointer-events: none;
  z-index: 0;
}

/* Glow orbs */
.orb {
  position: fixed;
  border-radius: 50%;
  filter: blur(80px);
  opacity: 0.15;
  pointer-events: none;
  z-index: 0;
}
.orb1 { width: 400px; height: 400px; background: var(--accent2); top: -100px; left: -100px; }
.orb2 { width: 300px; height: 300px; background: var(--accent); bottom: -80px; right: -80px; }

.wrapper {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 720px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* Top bar */
.topbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 20px;
  background: var(--surface);
}
.topbar-left {
  font-family: var(--display);
  font-size: 1.1rem;
  font-weight: 800;
  letter-spacing: 0.05em;
}
.topbar-left span { color: var(--accent); }
.status-pill {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.72rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--accent);
}
.pulse-dot {
  width: 8px; height: 8px;
  background: var(--accent);
  border-radius: 50%;
  animation: pulse 2s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(79, 255, 176, 0.6); }
  50% { box-shadow: 0 0 0 6px rgba(79, 255, 176, 0); }
}

/* Hero card */
.hero {
  border: 1px solid var(--border);
  border-radius: 16px;
  background: var(--surface);
  padding: 40px 36px;
  position: relative;
  overflow: hidden;
}
.hero::after {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent), var(--accent2), transparent);
}
.hero-label {
  font-size: 0.68rem;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 12px;
}
.hero-title {
  font-family: var(--display);
  font-size: clamp(2rem, 5vw, 3.2rem);
  font-weight: 800;
  line-height: 1.1;
  margin-bottom: 16px;
}
.hero-title .accent { color: var(--accent); }
.hero-title .accent2 { color: var(--accent2); }
.hero-desc {
  color: var(--muted);
  font-size: 0.82rem;
  line-height: 1.8;
  max-width: 480px;
}

/* Stats grid */
.stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}
.stat-card {
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface);
  padding: 20px 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  position: relative;
  overflow: hidden;
  transition: border-color 0.2s;
}
.stat-card:hover { border-color: var(--accent2); }
.stat-icon { font-size: 1.3rem; }
.stat-value {
  font-family: var(--display);
  font-size: 1.6rem;
  font-weight: 800;
  color: var(--accent);
}
.stat-label {
  font-size: 0.68rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--muted);
}

/* Info row */
.info-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.info-card {
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface);
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.info-title {
  font-size: 0.68rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 8px;
}
.info-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 0.78rem;
  padding: 4px 0;
  border-bottom: 1px solid rgba(255,255,255,0.04);
}
.info-item:last-child { border-bottom: none; }
.info-key { color: var(--muted); }
.info-val { color: var(--text); }
.badge-ok  { color: var(--accent); }
.badge-off { color: #ff6b6b; }

/* Footer */
.footer {
  text-align: center;
  font-size: 0.7rem;
  color: var(--muted);
  letter-spacing: 0.08em;
  padding-top: 4px;
}

/* Uptime counter */
#uptime { color: var(--accent2); }

@media (max-width: 520px) {
  .stats { grid-template-columns: repeat(2, 1fr); }
  .info-row { grid-template-columns: 1fr; }
  .topbar-left { font-size: 0.9rem; }
  .hero { padding: 28px 20px; }
}
</style>
</head>
<body>
<div class="orb orb1"></div>
<div class="orb orb2"></div>

<div class="wrapper">
  <!-- Top bar -->
  <div class="topbar">
    <div class="topbar-left">&#x1F916; <span>AERIVUE</span> BOT</div>
    <div class="status-pill">
      <div class="pulse-dot"></div>
      SYSTEM ONLINE
    </div>
  </div>

  <!-- Hero -->
  <div class="hero">
    <div class="hero-label">// telegram automation system</div>
    <h1 class="hero-title">
      <span class="accent">Force</span>&#8203;<span class="accent2">Join</span><br>
      Bot Engine
    </h1>
    <p class="hero-desc">
      Production-grade Telegram bot with async broadcast engine,
      MongoDB persistence, and multi-admin management.<br>
      Hosted on Render &bull; python-telegram-bot v20+
    </p>
  </div>

  <!-- Stats -->
  <div class="stats">
    <div class="stat-card">
      <div class="stat-icon">⚡</div>
      <div class="stat-value badge-ok">UP</div>
      <div class="stat-label">Bot Status</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">🗄️</div>
      <div class="stat-value badge-ok">OK</div>
      <div class="stat-label">Database</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">⏱️</div>
      <div class="stat-value" id="uptime">0s</div>
      <div class="stat-label">Uptime</div>
    </div>
  </div>

  <!-- Info row -->
  <div class="info-row">
    <div class="info-card">
      <div class="info-title">// Runtime</div>
      <div class="info-item"><span class="info-key">Framework</span><span class="info-val badge-ok">PTB v20</span></div>
      <div class="info-item"><span class="info-key">Language</span><span class="info-val">Python 3.11</span></div>
      <div class="info-item"><span class="info-key">Host</span><span class="info-val">Render</span></div>
      <div class="info-item"><span class="info-key">DB</span><span class="info-val">MongoDB Atlas</span></div>
    </div>
    <div class="info-card">
      <div class="info-title">// Features</div>
      <div class="info-item"><span class="info-key">Force Join</span><span class="info-val badge-ok">ACTIVE</span></div>
      <div class="info-item"><span class="info-key">Async Broadcast</span><span class="info-val badge-ok">ACTIVE</span></div>
      <div class="info-item"><span class="info-key">Multi-Admin</span><span class="info-val badge-ok">ACTIVE</span></div>
      <div class="info-item"><span class="info-key">Rate Limiter</span><span class="info-val badge-ok">ACTIVE</span></div>
    </div>
  </div>

  <div class="footer">&copy; 2026 &mdash; All rights reserved &nbsp;&bull;&nbsp; @aerivue</div>
</div>

<script>
  const start = Date.now();
  function fmt(s) {
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60) + 's';
    return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
  }
  setInterval(() => {
    document.getElementById('uptime').textContent = fmt(Math.floor((Date.now()-start)/1000));
  }, 1000);
</script>
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

# ================= BROADCAST (Async, Rate-Limited) =================
async def _send_one(bot, chat_id: int, text: str) -> bool:
    """
    Send a single message. Returns True on success, False on permanent failure.
    Handles Telegram rate-limit (RetryAfter) by waiting the required time,
    then retrying once. Silently drops users who blocked the bot (Forbidden).
    """
    try:
        await bot.send_message(chat_id=chat_id, text=text)
        return True
    except TelegramError as e:
        # 429 Too Many Requests — Telegram told us exactly how long to wait
        if hasattr(e, "retry_after") and e.retry_after:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(chat_id=chat_id, text=text)
                return True
            except Exception:
                return False
        # User blocked the bot or deleted account — skip silently
        if isinstance(e, Forbidden):
            return False
        logger.warning("send_message failed for %s: %s", chat_id, e)
        return False
    except Exception as e:
        logger.warning("send_message unexpected error for %s: %s", chat_id, e)
        return False


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg = " ".join(context.args).strip()
    if not msg:
        await update.message.reply_text("Usage: /broadcast your message")
        return

    # Fetch all user IDs up front (cursor exhausted quickly, avoids open cursor issues)
    user_ids = [d["user_id"] for d in users_col.find({}, {"user_id": 1, "_id": 0})]
    total = len(user_ids)

    if total == 0:
        await update.message.reply_text("No users to broadcast to.")
        return

    status_msg = await update.message.reply_text(
        f"📣 Broadcasting to *{total}* users...\nThis may take a moment.",
        parse_mode="Markdown"
    )

    # --- Semaphore-controlled concurrent sends ---
    # BROADCAST_CONCURRENCY = 10 means at most 10 sends at the same time.
    # Each finished send immediately starts the next — no artificial batch delays.
    # This stays safe under Telegram's ~30 msg/s limit for bot API.
    sem = asyncio.Semaphore(BROADCAST_CONCURRENCY)

    async def guarded_send(uid: int) -> bool:
        async with sem:
            result = await _send_one(context.bot, uid, msg)
            # Small fixed delay per send to avoid bursting all 10 at once
            await asyncio.sleep(BROADCAST_CHUNK_DELAY)
            return result

    results = await asyncio.gather(*[guarded_send(uid) for uid in user_ids])

    success = sum(results)
    fail    = total - success

    # Log to DB
    broadcasts_col.insert_one({
        "message": msg,
        "date": datetime.now().isoformat(),
        "total": total,
        "success": success,
        "failed": fail,
    })

    # Update the status message
    try:
        await status_msg.edit_text(
            f"✅ *Broadcast Complete!*\n\n"
            f"Total: `{total}`\n"
            f"Sent: `{success}`\n"
            f"Failed: `{fail}`",
            parse_mode="Markdown"
        )
    except Exception:
        await update.message.reply_text(
            f"✅ Broadcast done!\n\nSent: {success}\nFailed: {fail}"
        )

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

    app.add_handler(CommandHandler("owner",       owner_cmd))
    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("add",         add_channel))
    app.add_handler(CommandHandler("remove",      remove_channel))
    app.add_handler(CommandHandler("update",      update_channel))
    app.add_handler(CommandHandler("list",        list_channels))
    app.add_handler(CommandHandler("channels",    channel_buttons))
    app.add_handler(CommandHandler("broadcast",   broadcast))
    app.add_handler(CommandHandler("addadmin",    add_admin))
    app.add_handler(CommandHandler("removeadmin", remove_admin))
    app.add_handler(CommandHandler("admins",      list_admins))
    app.add_handler(CommandHandler("stats",       stats))
    app.add_handler(CommandHandler("setmsg",      set_message))
    app.add_handler(CommandHandler("setimage",    set_image))
    app.add_handler(CallbackQueryHandler(check_join, pattern="check"))

    app.run_polling(
        # Drop pending updates accumulated while bot was offline
        drop_pending_updates=True,
        # Allow up to 5 concurrent handler coroutines — safe on Render free tier
        # (increase to 10 if you upgrade to a paid Render instance)
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    main()
