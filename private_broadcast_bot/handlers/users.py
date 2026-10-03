from typing import Optional, Any
from telethon import events
from services.subscriber_service import SubscriberService

def register_user_handlers(
    bot,
    subscriber_service: SubscriberService,
    default_owner_id: Optional[Any] = None,
    bot_id: Optional[str] = None
):
    @bot.on(events.NewMessage(pattern=r"^/start(?:\s+.*)?$"))
    async def handle_start(evt):
        if not evt.is_private:
            return
        
        # Parse optional deep link start parameter (e.g. /start 2093321330 or /start owner_2093321330)
        owner_id = default_owner_id
        text = evt.raw_text or evt.text or ""
        parts = text.strip().split(maxsplit=1)
        if len(parts) > 1:
            param = parts[1].strip()
            if param.startswith("owner_"):
                owner_id = param[6:]
            elif param.startswith("channel_"):
                owner_id = param[8:]
            elif param.startswith("c_"):
                owner_id = param[2:]
            else:
                owner_id = param

        if owner_id:
            s_owner = str(owner_id).strip()
            if s_owner.isdigit() and len(s_owner) >= 10 and s_owner.startswith("100"):
                owner_id = f"-{s_owner}"

        user = await evt.get_sender()
        response_text = await subscriber_service.register_subscriber(
            telegram_user_id=evt.sender_id,
            chat_id=evt.chat_id,
            username=getattr(user, "username", None),
            first_name=getattr(user, "first_name", None),
            last_name=getattr(user, "last_name", None),
            owner_id=owner_id,
            bot_id=bot_id
        )
        await evt.respond(response_text)

    @bot.on(events.NewMessage(pattern=r"^/stop$"))
    async def handle_stop(evt):
        if not evt.is_private:
            return

        response_text = await subscriber_service.opt_out_subscriber(
            telegram_user_id=evt.sender_id,
            owner_id=default_owner_id,
            bot_id=bot_id
        )
        await evt.respond(response_text)

