import json
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from telethon import TelegramClient, errors
from telethon.tl.types import MessageMediaWebPage
from database.repository import Repository
from config import BATCH_SIZE, DELAY_BETWEEN_MESSAGES, logger

class BroadcastWorker:
    def __init__(self, client: TelegramClient, repo: Repository, clients_map: Optional[Dict[str, TelegramClient]] = None):
        self.default_client = client
        self.clients_map = clients_map or {}
        self.repo = repo
        self._is_running = False
        self._wake_event = asyncio.Event()

    def register_client(self, bot_id: str, client: TelegramClient):
        if bot_id:
            self.clients_map[bot_id] = client

    def get_client_for_job(self, bot_id: Optional[str] = None) -> TelegramClient:
        if bot_id and bot_id in self.clients_map:
            return self.clients_map[bot_id]
        return self.default_client

    def wake(self):
        """Signal worker to check for pending jobs immediately."""
        self._wake_event.set()

    async def start(self):
        """Main worker loop running in background."""
        self._is_running = True
        logger.info("[PRIVATE-BROADCAST] Broadcast worker started")

        while self._is_running:
            try:
                processed_any = await self._process_pending_jobs()
                if not processed_any:
                    # Wait for wake event or timeout 10s
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=10.0)
                        self._wake_event.clear()
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                logger.info("[PRIVATE-BROADCAST] Broadcast worker shutting down...")
                break
            except Exception as ex:
                logger.error(f"[PRIVATE-BROADCAST] Error in worker loop: {ex}")
                await asyncio.sleep(5)

    async def stop(self):
        self._is_running = False
        self._wake_event.set()

    async def _process_pending_jobs(self) -> bool:
        """Fetch and process unfinished broadcast jobs."""
        jobs = await self.repo.get_unfinished_jobs()
        if not jobs:
            return False

        for job in jobs:
            job_id = job["id"]
            logger.info(f"[PRIVATE-BROADCAST] Processing broadcast job: {job_id}")
            await self._run_job(job)
        return True

    async def _run_job(self, job: Dict[str, Any]):
        job_id = job["id"]
        source_chat_id = job["source_chat_id"]
        source_message_id = job["source_message_id"]
        message_type = job.get("message_type", "single")

        # Parse source_message_ids if album
        source_message_ids = [source_message_id]
        if job.get("source_message_ids"):
            try:
                raw_ids = job["source_message_ids"]
                if isinstance(raw_ids, str):
                    source_message_ids = json.loads(raw_ids)
                elif isinstance(raw_ids, list):
                    source_message_ids = raw_ids
            except Exception:
                source_message_ids = [source_message_id]

        # 1. Update job to processing
        if job["status"] == "pending":
            await self.repo.update_job(job_id, {
                "status": "processing",
                "started_at": datetime.now(timezone.utc).isoformat()
            })

        # 2. Get active subscribers & create delivery records if not already created
        owner_id = job.get("user_id")
        err_msg = job.get("error_message") or ""
        if not owner_id and "[owner:" in err_msg:
            try:
                owner_id = err_msg.split("[owner:")[1].split("]")[0]
            except Exception:
                pass
        if not owner_id and source_chat_id:
            owner_id = str(source_chat_id)

        subscribers = await self.repo.get_active_subscribers(
            owner_id=owner_id,
            bot_id=job.get("bot_id"),
            exclude_user_id=source_chat_id
        )
        if not subscribers and job.get("total_recipients", 0) == 0:
            logger.info(f"[PRIVATE-BROADCAST] No active subscribers found for job {job_id} (owner: {owner_id or 'default'})")
            await self.repo.update_job(job_id, {
                "status": "completed",
                "total_recipients": 0,
                "completed_at": datetime.now(timezone.utc).isoformat()
            })
            return

        # Ensure deliveries batch is populated
        await self.repo.create_deliveries_batch(job_id, subscribers)
        total_recipients = len(subscribers) if subscribers else job.get("total_recipients", 0)
        await self.repo.update_job(job_id, {"total_recipients": total_recipients})

        sent_count = job.get("sent_count", 0)
        failed_count = job.get("failed_count", 0)
        blocked_count = job.get("blocked_count", 0)

        # 3. Process deliveries in batches
        logger.info(f"[PRIVATE-BROADCAST] Broadcast started for job {job_id} (Target Subscribers: {total_recipients})")

        while True:
            pending_deliveries = await self.repo.get_pending_deliveries(job_id, limit=BATCH_SIZE)
            if not pending_deliveries:
                break

            for delivery in pending_deliveries:
                delivery_id = delivery["id"]
                target_user_id = delivery["telegram_user_id"]
                target_chat_id = delivery["chat_id"]

                # Mark delivery as sending immediately to prevent concurrent pickup or duplicate sends
                await self.repo.update_delivery(delivery_id, status="sending")

                success, status_code, err_msg = await self._send_to_user(
                    target_chat_id=target_chat_id,
                    target_user_id=target_user_id,
                    source_chat_id=source_chat_id,
                    source_message_ids=source_message_ids,
                    message_type=message_type,
                    bot_id=job.get("bot_id")
                )

                if success:
                    await self.repo.update_delivery(delivery_id, status="sent")
                    sent_count += 1
                elif status_code == "BLOCKED":
                    await self.repo.update_delivery(delivery_id, status="blocked", error_code="BLOCKED", error_message=err_msg)
                    await self.repo.deactivate_subscriber(target_user_id)
                    blocked_count += 1
                    logger.info(f"[PRIVATE-BROADCAST] User blocked bot: {target_user_id}")
                else:
                    await self.repo.update_delivery(delivery_id, status="failed", error_code=status_code, error_message=err_msg)
                    failed_count += 1

                # Rate limiting delay
                if DELAY_BETWEEN_MESSAGES > 0:
                    await asyncio.sleep(DELAY_BETWEEN_MESSAGES)

            # Update job progress periodically
            processed = sent_count + failed_count + blocked_count
            logger.info(f"[PRIVATE-BROADCAST] Progress: {processed}/{total_recipients} (Sent: {sent_count}, Failed: {failed_count}, Blocked: {blocked_count})")
            await self.repo.update_job(job_id, {
                "sent_count": sent_count,
                "failed_count": failed_count,
                "blocked_count": blocked_count
            })

        # 4. Finalize job completion
        await self.repo.update_job(job_id, {
            "status": "completed",
            "sent_count": sent_count,
            "failed_count": failed_count,
            "blocked_count": blocked_count,
            "completed_at": datetime.now(timezone.utc).isoformat()
        })
        logger.info(f"[PRIVATE-BROADCAST] Broadcast completed for job {job_id} | Total: {total_recipients}, Sent: {sent_count}, Failed: {failed_count}, Blocked: {blocked_count}")

    async def _send_to_user(
        self,
        target_chat_id: int,
        target_user_id: int,
        source_chat_id: int,
        source_message_ids: List[int],
        message_type: str,
        bot_id: Optional[str] = None
    ) -> tuple[bool, str, Optional[str]]:
        """
        Send/copy message to target Telegram user.
        Handles rate limits (FloodWait) and permanent errors (Bot Blocked).
        Resolves correct bot client instance for multi-tenant bots.
        """
        max_retries = 3
        retry_count = 0
        client = self.get_client_for_job(bot_id)

        while retry_count < max_retries:
            try:
                # Forward message with author attribution preserved (drop_author=False)
                if len(source_message_ids) == 1:
                    await client.forward_messages(
                        entity=target_chat_id,
                        messages=source_message_ids[0],
                        from_peer=source_chat_id,
                        drop_author=False
                    )
                else:
                    await client.forward_messages(
                        entity=target_chat_id,
                        messages=source_message_ids,
                        from_peer=source_chat_id,
                        drop_author=False
                    )
                return True, "SUCCESS", None

            except errors.FloodWaitError as fw:
                wait_time = fw.seconds + 1
                logger.warning(f"[PRIVATE-BROADCAST] Flood wait: {wait_time} seconds when messaging {target_user_id}")
                await asyncio.sleep(wait_time)
                retry_count += 1

            except (
                errors.UserIsBlockedError,
                errors.InputUserDeactivatedError,
                errors.UserDeactivatedError,
                errors.ChatWriteForbiddenError,
                errors.PeerIdInvalidError,
            ) as block_err:
                return False, "BLOCKED", str(block_err)

            except Exception as ex:
                err_str = str(ex).lower()
                if "blocked" in err_str or "forbidden" in err_str or "deactivated" in err_str:
                    return False, "BLOCKED", str(ex)
                
                # Fallback send logic if forward_messages failed due to content restrictions
                try:
                    msgs = await client.get_messages(source_chat_id, ids=source_message_ids)
                    # Resolve dynamic channel source title (e.g. 'test mb')
                    source_title = await self._resolve_source_channel_name(client, source_chat_id)
                    header_prefix = f"📢 **From {source_title}:**\n\n" if source_title else ""

                    if msgs:
                        msg_list = msgs if isinstance(msgs, list) else [msgs]
                        for m in msg_list:
                            if not m:
                                continue
                            text_content = m.message or ""
                            full_text = f"{header_prefix}{text_content}" if header_prefix else text_content
                            if m.media and not isinstance(m.media, MessageMediaWebPage):
                                await client.send_file(target_chat_id, file=m.media, caption=full_text)
                            elif full_text:
                                await client.send_message(target_chat_id, full_text)
                        return True, "SUCCESS_FALLBACK", None
                except Exception as fb_err:
                    logger.error(f"[PRIVATE-BROADCAST] Fallback send failed for user {target_user_id}: {fb_err}")

                retry_count += 1
                if retry_count < max_retries:
                    await asyncio.sleep(1.0)
                else:
                    return False, "FAILED", str(ex)

        return False, "FAILED", "Max retries exceeded"

    async def _resolve_source_channel_name(self, client: TelegramClient, source_chat_id: int) -> Optional[str]:
        """
        Dynamically resolve the source Channel Title (e.g. 'test mb') for message headers.
        Guarantees that user personal names (like 'Shwet') are never displayed.
        """
        try:
            # 1. Check tg_forward_mappings database table for mapped source_name or sender_name
            res = await self.repo.supabase.table("tg_forward_mappings").select("*").execute()
            data = getattr(res, "data", []) or []

            abs_chat_str = str(abs(source_chat_id))
            short_chat_str = abs_chat_str[3:] if abs_chat_str.startswith("100") else abs_chat_str

            for item in data:
                sid = item.get("sender_id")
                uid = item.get("user_id")
                
                matched = False
                if sid is not None and str(sid).lstrip("-").isdigit():
                    abs_s = str(abs(int(sid)))
                    short_s = abs_s[3:] if abs_s.startswith("100") else abs_s
                    if source_chat_id == int(sid) or short_chat_str == short_s:
                        matched = True
                if not matched and uid is not None and str(uid) == str(source_chat_id):
                    matched = True

                if matched:
                    src_name = item.get("source_name") or item.get("sender_name") or item.get("channel_name")
                    if src_name:
                        return str(src_name).strip()

            # 2. Query Telethon entity for channel title
            try:
                src_entity = await client.get_entity(source_chat_id)
                ch_title = getattr(src_entity, "title", None)
                if ch_title:
                    return str(ch_title).strip()
            except Exception:
                pass

            # 3. Query Telethon entity for mapped channel IDs from get_owner_aliases
            aliases = await self.repo.get_owner_aliases(source_chat_id)
            for a in aliases:
                if a.lstrip("-").isdigit() and (a.startswith("-100") or a.startswith("100")):
                    try:
                        c_id = int(a) if a.startswith("-") else int(f"-100{a[3:] if a.startswith('100') else a}")
                        c_ent = await client.get_entity(c_id)
                        c_title = getattr(c_ent, "title", None)
                        if c_title:
                            return str(c_title).strip()
                    except Exception:
                        pass

            return None
        except Exception as ex:
            logger.debug(f"[PRIVATE-BROADCAST] Could not resolve source channel name: {ex}")
            return None
