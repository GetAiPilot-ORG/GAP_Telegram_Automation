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

    running_bots = {}
    worker = None
    broadcast_service = None

    sessions_dir = os.path.join(BASE_DIR, "sessions")
    os.makedirs(sessions_dir, exist_ok=True)

    async def start_bot(b_info):
        nonlocal worker, broadcast_service
        try:
            bot_id = b_info.get("bot_id", "default")
            if bot_id == "default":
                session_path = os.path.join(sessions_dir, "local_private_broadcast_bot")
            else:
                session_path = os.path.join(sessions_dir, f"bot_{bot_id}")

            bot = TelegramClient(session_path, API_ID, API_HASH)
            await bot.start(bot_token=b_info["token"])
            me = await bot.get_me()
            owner_label = f"User ({b_info['owner_id']})" if b_info.get("owner_id") else "Shared Multi-Tenant System"
            logger.info(f"[PRIVATE-BROADCAST] Bot started: @{me.username} (ID: {me.id}) | Mode: {owner_label}")

            if not worker:
                worker = BroadcastWorker(bot, repo)
                broadcast_service = BroadcastService(repo, worker_trigger_callback=worker.wake)
                asyncio.create_task(worker.start())
            else:
                if not broadcast_service:
                    broadcast_service = BroadcastService(repo, worker_trigger_callback=worker.wake)

            if bot_id:
                worker.register_client(str(bot_id), bot)

            register_user_handlers(bot, subscriber_service, default_owner_id=b_info.get("owner_id"), bot_id=b_info.get("bot_id"))
            register_admin_handlers(bot, subscriber_service)
            register_relay_handlers(bot, broadcast_service)
            
            running_bots[bot_id] = bot
            asyncio.create_task(bot.run_until_disconnected())
        except Exception as ex:
            logger.error(f"[PRIVATE-BROADCAST] Failed to start bot session for {b_info.get('bot_id')}: {ex}")

    # Check local .env bot token first
    if BOT_TOKEN and "ABCdefGHIjklMNOpqrsTUVwxyZ" not in BOT_TOKEN:
        await start_bot({
            "token": BOT_TOKEN,
            "owner_id": None,
            "bot_id": "default"
        })

    # Dynamic bot manager loop
    async def bot_manager_loop():
        while True:
            try:
                db_bots = await repo.get_active_bots()
                for b in db_bots:
                    token = b.get("bot_token")
                    bot_id = str(b.get("id"))
                    owner_id = b.get("user_id")
                    
                    if token and token.strip().upper() != "SYSTEM_DEFAULT" and "ABCdef" not in token:
                        if bot_id not in running_bots:
                            await start_bot({
                                "token": token,
                                "owner_id": owner_id,
                                "bot_id": bot_id
                            })
            except Exception as e:
                logger.error(f"[PRIVATE-BROADCAST] Bot manager error: {e}")
            await asyncio.sleep(15)

    manager_task = asyncio.create_task(bot_manager_loop())
    logger.info("[PRIVATE-BROADCAST] Dynamic Multi-Tenant engine is running and watching for new bots...")

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        logger.info("[PRIVATE-BROADCAST] Shutting down Private Broadcast Bot Multi-Tenant engine...")
        if worker:
            await worker.stop()
        manager_task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("[PRIVATE-BROADCAST] Bot process stopped.")
