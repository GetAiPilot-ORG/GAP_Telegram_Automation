import os
import re
import base64
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import Application, CommandHandler, ChatJoinRequestHandler, ContextTypes

load_dotenv()

# ---------------- ENV ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("VITE_SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing BOT_TOKEN / SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

SUPABASE_URL = SUPABASE_URL.rstrip("/")

# ---------------- LOG ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("getaipilot-bot")

# ---------------- SUPABASE REST HELPERS ----------------
def sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def sb_get_single(table: str, select: str, filters: list[tuple[str, str, str]]):
    """
    filters: [(col, op, value)] where op in {"eq","gt","lt","gte","lte","like","ilike","neq"}
    """
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params = {"select": select, "limit": "1"}
    for col, op, val in filters:
        params[col] = f"{op}.{val}"
    r = requests.get(url, headers=sb_headers(), params=params, timeout=20)
    if not r.ok:
        log.error(f"Supabase GET failed {table}: {r.status_code} {r.text}")
        return None
    data = r.json()
    return data[0] if isinstance(data, list) and data else None

def sb_get_many(table: str, select: str, filters: list[tuple[str, str, str]]):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params = {"select": select}
    for col, op, val in filters:
        params[col] = f"{op}.{val}"
    r = requests.get(url, headers=sb_headers(), params=params, timeout=30)
    if not r.ok:
        log.error(f"Supabase GET many failed {table}: {r.status_code} {r.text}")
        return []
    data = r.json()
    return data if isinstance(data, list) else []

def sb_update(table: str, values: dict, filters: list[tuple[str, str, str]]):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params = {}
    for col, op, val in filters:
        params[col] = f"{op}.{val}"
    # Prefer return=minimal to reduce payload
    headers = sb_headers()
    headers["Prefer"] = "return=minimal"
    r = requests.patch(url, headers=headers, params=params, json=values, timeout=20)
    if not r.ok:
        log.error(f"Supabase UPDATE failed {table}: {r.status_code} {r.text}")
        return False
    return True

# ---------------- CACHE ----------------
COMMUNITY_CACHE: dict[int, dict] = {}

# ---------------- HELPERS ----------------
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)

def now_dt():
    return datetime.now(timezone.utc)

def now_iso():
    return now_dt().isoformat()

def parse_ts(value):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip().replace(" ", "T")
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def parse_start_payload(text: str | None):
    if not text:
        return None
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return None
    payload = parts[1].strip()
    if not payload or len(payload) > 200:
        return None
    return payload

def try_b64_decode(s: str):
    try:
        padded = s + "=" * (-len(s) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("utf-8"))
        decoded = raw.decode("utf-8").strip()
        return decoded or None
    except Exception:
        return None

def is_uuid(s: str):
    return bool(UUID_RE.fullmatch(s))

async def get_community_by_chat_id(chat_id: int):
    if chat_id in COMMUNITY_CACHE:
        return COMMUNITY_CACHE[chat_id]
    community = sb_get_single(
        "tg_communities",
        "id,title,telegram_chat_id,invite_link,is_active,join_mode",
        [("telegram_chat_id", "eq", str(chat_id))],
    )
    COMMUNITY_CACHE[chat_id] = community
    return community

# ---------------- CORE: /start <payload> ----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    raw_payload = parse_start_payload(msg.text)
    if not raw_payload:
        await msg.reply_text("❌ Please open the bot using your payment link again.")
        return

    decoded_payload = try_b64_decode(raw_payload)
    payloads_to_try = [raw_payload]
    if decoded_payload and decoded_payload != raw_payload:
        payloads_to_try.append(decoded_payload)

    tg_user_id = user.id
    log.info(f"/start payload received | tg_user={tg_user_id} raw={raw_payload} decoded={decoded_payload}")

    deeplink = None
    subscription_id = None

    # 1) Try UUID direct
    for p in payloads_to_try:
        if is_uuid(p):
            subscription_id = p
            break

    # 2) Else token lookup in telegram_deeplinks
    if not subscription_id:
        for p in payloads_to_try:
            deeplink = sb_get_single(
                "tg_deeplinks",
                "id,token,subscription_id,used_at",
                [("token", "eq", p)],
            )
            if deeplink:
                if deeplink.get("used_at"):
                    await msg.reply_text("❌ This link was already used.")
                    return
                subscription_id = deeplink.get("subscription_id")
                break

    if not subscription_id:
        await msg.reply_text("❌ Invalid link. (token/subscription not found)")
        return

    # 3) Load subscription with embedded communities
    sub = sb_get_single(
        "tg_channel_subscriptions",
        "id,status,expires_at,community_id,landing_page_id,plan_id,"
        "tg_communities(id,title,invite_link,is_active,join_mode,telegram_chat_id)",
        [("id", "eq", str(subscription_id))],
    )

    if not sub:
        await msg.reply_text(
            "❌ Subscription not found.\n\n"
            "Admin ko bolo: `tg_channel_subscriptions` row + `tg_deeplinks` row create ho rahi hai ya nahi."
        )
        return

    status = (sub.get("status") or "active").lower()
    exp_dt = parse_ts(sub.get("expires_at"))
    if status != "active" or not exp_dt or exp_dt <= now_dt():
        await msg.reply_text("❌ Subscription inactive/expired.")
        return

    community = sub.get("tg_communities")
    if isinstance(community, list):
        community = community[0] if community else None

    if not community:
        # fallback by community_id
        cid = sub.get("community_id")
        if cid:
            community = sb_get_single(
                "tg_communities",
                "id,title,telegram_chat_id,invite_link,is_active,join_mode",
                [("id", "eq", str(cid))],
            )

    if not community:
        await msg.reply_text("❌ Community not found / mapping issue. Admin ko bolo community_id check kare.")
        return

    if not community.get("is_active", True):
        await msg.reply_text("❌ Channel is currently inactive.")
        return

    invite_link = community.get("invite_link")
    if not invite_link:
        await msg.reply_text("✅ Verified, but invite link missing. Admin ko bolo invite_link update kare.")
        return

    # 4) Map telegram_user_id in channel_subscriptions
    ok = sb_update(
        "tg_channel_subscriptions",
        {"telegram_user_id": tg_user_id},
        [("id", "eq", str(subscription_id))],
    )
    if not ok:
        await msg.reply_text("❌ Could not link Telegram with subscription. Try again.")
        return

    # 5) Mark deeplink used_at (one-time)
    if deeplink and deeplink.get("id"):
        sb_update("tg_deeplinks", {"used_at": now_iso()}, [("id", "eq", str(deeplink["id"]))])

    title = community.get("title") or "your channel"
    await msg.reply_text(
        f"✅ Subscription verified!\n\n"
        f"Join {title} 👇\n\n"
        f"{invite_link}"
    )

# ---------------- JOIN REQUEST AUTO-APPROVE ----------------
async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if not req:
        return

    chat_id = req.chat.id
    tg_user_id = req.from_user.id

    community = await get_community_by_chat_id(chat_id)
    if not community or not community.get("is_active", True):
        return

    if (community.get("join_mode") or "request") != "request":
        return

    rows = sb_get_many(
        "tg_channel_subscriptions",
        "id,expires_at,status",
        [
            ("telegram_user_id", "eq", str(tg_user_id)),
            ("community_id", "eq", str(community["id"])),
            ("status", "eq", "active"),
            ("expires_at", "gt", now_iso()),
        ],
    )
    valid = rows[0] if rows else None

    if not valid:
        try:
            await context.bot.decline_chat_join_request(chat_id, tg_user_id)
        except Exception:
            pass
        return

    try:
        await context.bot.approve_chat_join_request(chat_id, tg_user_id)
        log.info(f"Approved join request chat={chat_id} tg_user={tg_user_id} sub={valid['id']}")
    except Exception as e:
        log.error(f"approve join request failed: {e}")

# ---------------- EXPIRY JOB ----------------
async def expiry_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        rows = sb_get_many(
            "tg_channel_subscriptions",
            "id,telegram_user_id,status,expires_at,community_id,tg_communities(telegram_chat_id,title)",
            [("status", "eq", "active")],
        )
        now_utc = now_dt()

        for sub in rows:
            uid = sub.get("telegram_user_id")
            community = sub.get("tg_communities")
            if isinstance(community, list):
                community = community[0] if community else None
            if not uid or not community:
                continue

            exp_dt = parse_ts(sub.get("expires_at"))
            if not exp_dt or exp_dt > now_utc:
                continue

            chat_id = community.get("telegram_chat_id")
            if not chat_id:
                continue

            try:
                await context.bot.ban_chat_member(chat_id, uid)
                await context.bot.unban_chat_member(chat_id, uid)
            except Exception:
                pass

            sb_update("tg_channel_subscriptions", {"status": "expired"}, [("id", "eq", str(sub["id"]))])

    except Exception as e:
        log.error(f"expiry_job error: {e}")

# ---------------- MAIN ----------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    app.job_queue.run_repeating(expiry_job, interval=120, first=20)
    log.info("🚀 Bot running: verify → map tg_user_id → send invite link → auto-approve → expiry kick")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
