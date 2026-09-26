import asyncio
from typing import Dict, List, Optional, Any, Callable
from database.repository import Repository
from config import RELAY_CHAT_IDS, ADMIN_IDS, logger

class BroadcastService:
    def __init__(self, repo: Repository, worker_trigger_callback: Optional[Callable] = None):
        self.repo = repo
        self.worker_trigger_callback = worker_trigger_callback
        # In-memory buffer for album media groups
        # key: (source_chat_id, grouped_id) -> {"messages": [...], "task": asyncio.TimerHandle}
        self._album_buffers: Dict[tuple, Dict[str, Any]] = {}

    def is_authorized_relay(self, chat_id: int, sender_id: Optional[int] = None) -> bool:
        """
        Check if chat_id matches configured relay source OR sender_id is an authorized admin.
        Handles -100 prefix variations in Telegram channel IDs.
        """
        if RELAY_CHAT_IDS:
            chat_str = str(chat_id)
            abs_chat_str = str(abs(chat_id))
            if abs_chat_str.startswith("100"):
                short_chat_str = abs_chat_str[3:]
            else:
                short_chat_str = abs_chat_str

            for r_id in RELAY_CHAT_IDS:
                r_str = str(r_id)
                abs_r_str = str(abs(r_id))
                short_r_str = abs_r_str[3:] if abs_r_str.startswith("100") else abs_r_str

                if chat_id == r_id or chat_str == r_str or short_chat_str == short_r_str:
                    return True

        if ADMIN_IDS and sender_id is not None and sender_id in ADMIN_IDS and not RELAY_CHAT_IDS:
            # Only authorize ADMIN_IDS if no specific RELAY_CHAT_IDS are configured AND chat is not a private user chat
            pass
        return False

    async def is_authorized_relay_async(self, chat_id: int, sender_id: Optional[int] = None) -> bool:
        """
        Async check authorizing relay source against RELAY_CHAT_IDS, ADMIN_IDS,
        AND AutoForward mappings in tg_forward_mappings database table!
        """
        if self.is_authorized_relay(chat_id, sender_id):
            return True

        # Auto-authorize if chat matches an incoming source mapped in AutoForward
        af_ids = await self.repo.get_autoforward_source_ids()
        if af_ids:
            abs_chat_str = str(abs(chat_id))
            short_chat_str = abs_chat_str[3:] if abs_chat_str.startswith("100") else abs_chat_str

            for af_id in af_ids:
                abs_af_str = str(abs(af_id))
                short_af_str = abs_af_str[3:] if abs_af_str.startswith("100") else abs_af_str

                if chat_id == af_id or short_chat_str == short_af_str:
                    return True

        return False

    async def handle_relay_message(self, message, sender_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Entry point for relay messages.
        Distinguishes single messages from album media groups.
        Resolves owner_id from tg_forward_mappings for strict subscriber isolation.
        """
        chat_id = message.chat_id
        if not await self.is_authorized_relay_async(chat_id, sender_id):
            logger.warning(f"[PRIVATE-BROADCAST] Ignored message from unauthorized chat_id: {chat_id}, sender_id: {sender_id}")
            return None

        # Fetch mapping to resolve owner_id
        mapping = await self.repo.get_autoforward_mapping_for_source(chat_id)
        owner_id = mapping.get("user_id") if mapping else sender_id

        grouped_id = getattr(message, "grouped_id", None)
        msg_id = message.id

        logger.info(f"[PRIVATE-BROADCAST] Incoming relay message: {msg_id} (chat: {chat_id}, album_id: {grouped_id}, owner: {owner_id or 'default'})")

        if grouped_id:
            # Handle album buffering
            await self._buffer_album_message(chat_id, grouped_id, message, owner_id=owner_id)
            return None
        else:
            # Handle single message
            return await self._create_single_broadcast_job(chat_id, msg_id, owner_id=owner_id)

    async def _buffer_album_message(self, chat_id: int, grouped_id: int, message, owner_id: Optional[Any] = None):
        """
        Collect messages belonging to the same media group, then trigger job creation after delay.
        """
        key = (chat_id, grouped_id)
        if key not in self._album_buffers:
            self._album_buffers[key] = {
                "messages": [],
                "task": None,
                "owner_id": owner_id
            }

        buf = self._album_buffers[key]
        # Avoid duplicate message IDs in album buffer
        if not any(m.id == message.id for m in buf["messages"]):
            buf["messages"].append(message)

        # Cancel previous timer if still pending
        if buf["task"] and not buf["task"].done():
            buf["task"].cancel()

        # Schedule album processing after 1.5 seconds window
        buf["task"] = asyncio.create_task(self._finalize_album_buffer(key))

    async def _finalize_album_buffer(self, key: tuple):
        """
        Wait for album items to complete, then create an album broadcast job.
        """
        try:
            await asyncio.sleep(1.5)
            buf = self._album_buffers.pop(key, None)
            if not buf or not buf["messages"]:
                return

            chat_id, grouped_id = key
            owner_id = buf.get("owner_id")
            messages = buf["messages"]
            # Sort message IDs sequentially
            messages.sort(key=lambda m: m.id)
            msg_ids = [m.id for m in messages]
            first_msg_id = msg_ids[0]

            logger.info(f"[PRIVATE-BROADCAST] Finalized album {grouped_id} with {len(msg_ids)} messages (owner: {owner_id or 'default'})")

            job = await self.repo.create_broadcast_job(
                source_chat_id=chat_id,
                source_message_id=first_msg_id,
                grouped_id=grouped_id,
                message_type="album",
                source_message_ids=msg_ids,
                owner_id=owner_id
            )

            if job and self.worker_trigger_callback:
                self.worker_trigger_callback()
        except asyncio.CancelledError:
            pass
        except Exception as ex:
            logger.error(f"[PRIVATE-BROADCAST] Error finalizing album buffer {key}: {ex}")

    async def _create_single_broadcast_job(self, chat_id: int, msg_id: int, owner_id: Optional[Any] = None) -> Optional[Dict[str, Any]]:
        """
        Create broadcast job for a single message.
        """
        job = await self.repo.create_broadcast_job(
            source_chat_id=chat_id,
            source_message_id=msg_id,
            grouped_id=None,
            message_type="single",
            source_message_ids=[msg_id],
            owner_id=owner_id
        )
        if job and self.worker_trigger_callback:
            self.worker_trigger_callback()
        return job
