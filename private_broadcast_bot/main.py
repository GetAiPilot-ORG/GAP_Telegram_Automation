import os
import sys
import asyncio
import signal
from supabase import create_async_client, AsyncClient
from telethon import TelegramClient
from telethon.sessions import MemorySession

from config import (
    BASE_DIR,
    API_ID,
    API_HASH,
    BOT_TOKEN,
    SUPABASE_URL,
    SUPABASE_KEY,
    validate_config,
    logger
)
from database.repository import Repository
from services.subscriber_service import SubscriberService
from services.broadcast_service import BroadcastService
from workers.broadcast_worker import BroadcastWorker

from handlers.users import register_user_handlers
from handlers.admin import register_admin_handlers
from handlers.relay import register_relay_handlers

async def main():
    logger.info("[PRIVATE-BROADCAST] Starting Private Broadcast Bot Multi-Tenant Service...")
    
    # 1. Initialize Supabase Async Client
    try:
        supabase: AsyncClient = await create_async_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("[PRIVATE-BROADCAST] Supabase async client connected")
    except Exception as ex:
        logger.error(f"[PRIVATE-BROADCAST] Failed to initialize Supabase client: {ex}")
        sys.exit(1)

    repo = Repository(supabase)
    subscriber_service = SubscriberService(repo)

    bots_to_run = []

    # Check local .env bot token
    if BOT_TOKEN and "ABCdefGHIjklMNOpqrsTUVwxyZ" not in BOT_TOKEN:
        bots_to_run.append({
            "token": BOT_TOKEN,
            "owner_id": None,
            "bot_id": "default"
        })

    # Fetch active multi-tenant bots registered via Dashboard
    db_bots = await repo.get_active_bots()
    for b in db_bots:
        token = b.get("bot_token")
        if token and token != "SYSTEM_DEFAULT" and "ABCdefGHIjklMNOpqrsTUVwxyZ" not in token and token not in [bt["token"] for bt in bots_to_run]:
            bots_to_run.append({
                "token": token,
                "owner_id": b.get("user_id"),
                "bot_id": str(b.get("id"))
            })

    if not bots_to_run:
        logger.warning("[PRIVATE-BROADCAST] No active bot tokens found in .env or Supabase table 'telegram_private_broadcast_bots'. Waiting...")

    active_clients = []
    worker = None

    sessions_dir = os.path.join(BASE_DIR, "sessions")
    os.makedirs(sessions_dir, exist_ok=True)

    for b_info in bots_to_run:
        try:
            bot_id = b_info.get("bot_id", "default")
            if bot_id == "default":
                session_path = os.path.join(sessions_dir, "local_private_broadcast_bot")
            else:
                session_path = os.path.join(sessions_dir, f"bot_{bot_id}")

            bot = TelegramClient(session_path, API_ID, API_HASH)
            await bot.start(bot_token=b_info["token"])
            me = await bot.get_me()
            logger.info(f"[PRIVATE-BROADCAST] Bot started: @{me.username} (ID: {me.id}) | Owner: {b_info.get('owner_id') or 'default'}")

            if not worker:
                worker = BroadcastWorker(bot, repo)
                broadcast_service = BroadcastService(repo, worker_trigger_callback=worker.wake)
            else:
                broadcast_service = BroadcastService(repo, worker_trigger_callback=worker.wake)

            register_user_handlers(
                bot,
                subscriber_service,
                default_owner_id=b_info.get("owner_id"),
                bot_id=b_info.get("bot_id")
            )
            register_admin_handlers(bot, subscriber_service)
            register_relay_handlers(bot, broadcast_service)
            active_clients.append(bot)
        except Exception as ex:
            logger.error(f"[PRIVATE-BROADCAST] Failed to start bot session: {ex}")

    if worker:
        worker_task = asyncio.create_task(worker.start())

    logger.info(f"[PRIVATE-BROADCAST] Multi-Tenant engine running with {len(active_clients)} active bot(s)")

    try:
        if active_clients:
            await asyncio.gather(*[client.run_until_disconnected() for client in active_clients])
        else:
            while True:
                await asyncio.sleep(10)
    finally:
        logger.info("[PRIVATE-BROADCAST] Shutting down Private Broadcast Bot Multi-Tenant engine...")
        if worker:
            await worker.stop()
            worker_task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("[PRIVATE-BROADCAST] Bot process stopped.")
