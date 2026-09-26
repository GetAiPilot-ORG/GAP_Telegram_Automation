from telethon import events
from services.broadcast_service import BroadcastService
from config import logger

def register_relay_handlers(bot, broadcast_service: BroadcastService):
    @bot.on(events.NewMessage(incoming=True))
    async def handle_incoming_relay(evt):
        # Ignore outgoing messages sent by the bot itself
        if evt.out:
            return

        # Ignore messages sent by the bot ID
        try:
            me = await bot.get_me()
            if me and (evt.sender_id == me.id or evt.chat_id == me.id):
                return
        except Exception:
            pass

        # Skip commands
        if evt.text and evt.text.startswith("/"):
            return

        chat_id = evt.chat_id
        sender_id = evt.sender_id

        if not await broadcast_service.is_authorized_relay_async(chat_id, sender_id):
            return

        # Process authorized relay message
        job = await broadcast_service.handle_relay_message(evt.message, sender_id=sender_id)

        # If sent by admin directly in private chat, send confirmation response
        if evt.is_private and job:
            await evt.respond("🚀 **Broadcast job created!** Forwarding post to active subscribers...")
