from telethon import events
from services.subscriber_service import SubscriberService

def register_admin_handlers(bot, subscriber_service: SubscriberService):
    @bot.on(events.NewMessage(pattern=r"^/stats$"))
    async def handle_stats(evt):
        if not evt.is_private:
            return

        response_text = await subscriber_service.get_admin_stats(evt.sender_id)
        await evt.respond(response_text)
