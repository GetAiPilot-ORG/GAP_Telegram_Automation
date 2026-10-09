from typing import Dict, Optional, Any
from database.repository import Repository
from config import WELCOME_MESSAGE, ADMIN_IDS, logger

class SubscriberService:
    def __init__(self, repo: Repository):
        self.repo = repo

    async def register_subscriber(
        self,
        telegram_user_id: int,
        chat_id: int,
        username: Optional[str],
        first_name: Optional[str],
        last_name: Optional[str],
        owner_id: Optional[Any] = None,
        bot_id: Optional[str] = None
    ) -> str:
        """
        Register or reactivate subscriber upon /start.
        """
        success = await self.repo.upsert_subscriber(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            owner_id=owner_id,
            bot_id=bot_id
        )
        if success:
            logger.info(f"[PRIVATE-BROADCAST] Subscriber registered: {telegram_user_id} (@{username or 'no_username'}) | Owner: {owner_id or 'default'}")
            custom_msg = await self.repo.get_custom_welcome_message(owner_id=owner_id, bot_id=bot_id)
            if custom_msg:
                try:
                    import json
                    parsed = json.loads(custom_msg)
                    if isinstance(parsed, list) and len(parsed) > 0:
                        return parsed
                except Exception:
                    pass
                return [custom_msg]
            return [WELCOME_MESSAGE]
        else:
            return ["⚠️ An error occurred while processing your subscription. Please try again later."]

    async def opt_out_subscriber(self, telegram_user_id: int, owner_id: Optional[Any] = None, bot_id: Optional[str] = None) -> str:
        """
        Deactivate subscriber upon /stop.
        """
        success = await self.repo.deactivate_subscriber(telegram_user_id)
        if success:
            logger.info(f"[PRIVATE-BROADCAST] Subscriber opted out: {telegram_user_id}")
            return "⏸️ **You have been unsubscribed.**\n\nYou will no longer receive broadcast messages. Send **/start** anytime to reactivate your subscription."
        else:
            return "⚠️ An error occurred while processing your request."

    async def get_admin_stats(self, requesting_user_id: int) -> str:
        """
        Generate /stats summary for authorized admins.
        """
        if ADMIN_IDS and requesting_user_id not in ADMIN_IDS:
            return "❌ You are not authorized to view admin statistics."

        counts = await self.repo.get_subscriber_counts()
        recent_job = await self.repo.get_recent_job_stats()

        text = (
            "📊 **Private Broadcast Statistics**\n\n"
            f"👥 **Subscribers:**\n"
            f"• Total: `{counts['total']}`\n"
            f"• Active: `{counts['active']}`\n"
            f"• Inactive/Blocked: `{counts['inactive']}`\n\n"
        )

        if recent_job:
            status_emoji = {
                "pending": "⏳",
                "processing": "🔄",
                "completed": "✅",
                "failed": "❌"
            }.get(recent_job.get("status", ""), "ℹ️")

            text += (
                f"📡 **Latest Broadcast Job:**\n"
                f"• Status: {status_emoji} `{recent_job.get('status', 'unknown')}`\n"
                f"• Total Recipients: `{recent_job.get('total_recipients', 0)}`\n"
                f"• Sent: `{recent_job.get('sent_count', 0)}`\n"
                f"• Failed: `{recent_job.get('failed_count', 0)}`\n"
                f"• Blocked: `{recent_job.get('blocked_count', 0)}`\n"
                f"• Created: `{recent_job.get('created_at', 'N/A')[:19]}`\n"
            )
            if recent_job.get("completed_at"):
                text += f"• Completed: `{recent_job.get('completed_at')[:19]}`\n"
        else:
            text += "📡 **Latest Broadcast Job:** No jobs recorded yet."

        return text
