# Private Broadcast Bot

A high-performance, restart-safe Telegram bot service designed for broadcasting channel updates to private subscriber chats via a private relay channel.

---

## 🎯 Architecture & Message Flow

```
SOURCE CHANNEL
      ↓
Existing AutoForward Bot
      ↓
Private Relay Channel / Group (PRIVATE_BROADCAST_RELAY_CHAT_ID)
      ↓
private_broadcast_bot (Relay Handler & Media Group Buffer)
      ↓
Supabase Database (Jobs & Idempotent Delivery Tracking)
      ↓
Asynchronous Broadcast Engine (Batching, Rate Limiting & Flood Wait handling)
      ↓
ACTIVE SUBSCRIBERS ONLY (/start)
      ↓
Private User Telegram Chats
```

---

## 🚀 Key Features

1. **User Subscription & Opt-Out**:
   - `/start` registers or reactivates subscribers (`is_active = True`).
   - `/stop` deactivates subscribers (`is_active = False`) without deleting data.

2. **Clean Message Copying**:
   - Preserves message formatting, entities, captions, media, inline keyboards, stickers, documents, and polls without adding `"Forwarded from..."` headers.

3. **Media Group / Album Support**:
   - Buffers grouped messages automatically before job creation, broadcasting complete albums in single clean posts without duplicate delivery.

4. **Rate Limiting & Failure Recovery**:
   - Bounded rate-limiting protects against Telegram spam restrictions.
   - Automatically catches `FloodWaitError` and sleeps for required duration.
   - Detects user bot bans (`UserIsBlockedError`) and auto-deactivates blocked subscribers.

5. **Crash & Restart Safety**:
   - Database job and delivery tracking (`pending`, `processing`, `sent`, `blocked`, `failed`) ensures resuming unfinished broadcasts will **NEVER** duplicate messages to users who already received them.

6. **Admin Telemetry**:
   - `/stats` command restricted to `PRIVATE_BROADCAST_ADMIN_IDS` displays live subscriber metrics and latest broadcast job progress.

---

## 📋 Setup & Installation

### 1. Database Migration
Run `schema.sql` in your Supabase SQL Editor:
- `telegram_private_broadcast_users`
- `telegram_private_broadcast_jobs`
- `telegram_private_broadcast_deliveries`

### 2. Environment Variables
Create `.env` in `private_broadcast_bot/`:

```env
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=your_api_hash
PRIVATE_BROADCAST_BOT_TOKEN=your_bot_token_from_botfather
PRIVATE_BROADCAST_RELAY_CHAT_ID=-1001234567890
PRIVATE_BROADCAST_ADMIN_IDS=123456789

SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_supabase_service_role_or_anon_key
```

### 3. Run Locally
```bash
pip install -r requirements.txt
python main.py
```

### 4. Deploy via PM2
```bash
pm2 start ecosystem.config.js
pm2 status
pm2 logs private-broadcast-bot
```
