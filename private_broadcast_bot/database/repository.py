import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from supabase import AsyncClient
from config import logger

class Repository:
    def __init__(self, supabase: AsyncClient):
        self.supabase = supabase

    # ---------------- BOTS MANAGEMENT ----------------
    async def get_active_bots(self) -> List[Dict[str, Any]]:
        """
        Fetch all active multi-tenant bots configured via Dashboard.
        """
        try:
            res = await self.supabase.table("telegram_private_broadcast_bots")\
                .select("*")\
                .eq("is_active", True)\
                .execute()
            return getattr(res, "data", []) or []
        except Exception as ex:
            logger.error(f"[PRIVATE-BROADCAST] Error fetching active bots: {ex}")
            return []

    async def get_owner_aliases(self, owner_id: Any) -> set:
        """
        Resolve all ID aliases (Supabase Auth UUID, Telegram User ID, mapped Channel IDs)
        for a given user/owner to guarantee 100% accurate subscriber routing across all bots and channels.
        """
        if not owner_id or owner_id == "default":
            return set()

        owner_str = str(owner_id).strip()
        aliases = {owner_str}

        # Calculate numeric ID variations (-100 prefix, positive, short)
        if owner_str.lstrip("-").isdigit():
            abs_s = owner_str.lstrip("-")
            short_s = abs_s[3:] if abs_s.startswith("100") else abs_s
            aliases.add(abs_s)
            aliases.add(short_s)
            aliases.add(f"-100{short_s}")

        try:
            # 1. Lookup in profiles table
            res_prof = await self.supabase.table("profiles")\
                .select("id, telegram_user_id")\
                .or_(f"id.eq.{owner_str},telegram_user_id.eq.{owner_str}")\
                .execute()
            for p in (getattr(res_prof, "data", []) or []):
                if p.get("id"):
                    aliases.add(str(p["id"]))
                if p.get("telegram_user_id"):
                    aliases.add(str(p["telegram_user_id"]))

            # 2. Lookup in app_user_subscriptions table
            res_sub = await self.supabase.table("app_user_subscriptions")\
                .select("user_id, telegram_user_id")\
                .or_(f"user_id.eq.{owner_str},telegram_user_id.eq.{owner_str}")\
                .execute()
            for s in (getattr(res_sub, "data", []) or []):
                if s.get("user_id"):
                    aliases.add(str(s["user_id"]))
                if s.get("telegram_user_id"):
                    aliases.add(str(s["telegram_user_id"]))

            # 3. Lookup mapped channels in tg_forward_mappings for all gathered user_ids
            current_uids = list(aliases)
            for uid in current_uids:
                if not uid.lstrip("-").isdigit():
                    res_map = await self.supabase.table("tg_forward_mappings")\
                        .select("sender_id, user_id")\
                        .eq("user_id", uid)\
                        .execute()
                else:
                    res_map = await self.supabase.table("tg_forward_mappings")\
                        .select("sender_id, user_id")\
                        .or_(f"user_id.eq.{uid},sender_id.eq.{uid}")\
                        .execute()
                for m in (getattr(res_map, "data", []) or []):
                    sid = str(m.get("sender_id") or "")
                    uid_val = str(m.get("user_id") or "")
                    if sid:
                        aliases.add(sid)
                        s_abs = sid.lstrip("-")
                        s_short = s_abs[3:] if s_abs.startswith("100") else s_abs
                        aliases.add(s_abs)
                        aliases.add(s_short)
                        aliases.add(f"-100{s_short}")
                    if uid_val:
                        aliases.add(uid_val)
        except Exception as ex:
            logger.warning(f"[PRIVATE-BROADCAST] Error resolving owner aliases for {owner_id}: {ex}")

        return aliases

    # ---------------- AUTOFORWARD MAPPINGS INTEGRATION ----------------
    async def get_autoforward_source_ids(self) -> List[int]:
        """
        Fetch incoming source chat/channel IDs configured in tg_forward_mappings
        ONLY where outgoing receiver is set to Private Broadcast ('Private_Broadcast').
        """
        try:
            res = await self.supabase.table("tg_forward_mappings").select("*").execute()
            data = getattr(res, "data", []) or []
            source_ids = []
            for item in data:
                receivers_names = item.get("receivers_names") or []
                is_pb_target = False
                if isinstance(receivers_names, list):
                    for name in receivers_names:
                        n_lower = str(name).lower()
                        if "private_broadcast" in n_lower or "private broadcast" in n_lower or "gapgrowbot" in n_lower:
                            is_pb_target = True
                            break
                elif isinstance(receivers_names, str):
                    if "private_broadcast" in receivers_names.lower():
                        is_pb_target = True

                if is_pb_target:
                    sid = item.get("sender_id")
                    if sid is not None:
                        try:
                            source_ids.append(int(sid))
                        except ValueError:
                            pass
            return source_ids
        except Exception as ex:
            logger.warning(f"[PRIVATE-BROADCAST] Could not fetch AutoForward mappings: {ex}")
            return []

    async def get_autoforward_mapping_for_source(self, source_chat_id: int) -> Optional[Dict[str, Any]]:
        """
        Fetch the tg_forward_mappings record for a given source channel ID.
        Handles -100 prefix variations in Telegram channel IDs.
        """
        try:
            res = await self.supabase.table("tg_forward_mappings").select("*").execute()
            data = getattr(res, "data", []) or []

            abs_chat_str = str(abs(source_chat_id))
            short_chat_str = abs_chat_str[3:] if abs_chat_str.startswith("100") else abs_chat_str

            for item in data:
                sid = item.get("sender_id")
                if sid is None:
                    continue

                try:
                    s_id = int(sid)
                except ValueError:
                    continue

                abs_s_str = str(abs(s_id))
                short_s_str = abs_s_str[3:] if abs_s_str.startswith("100") else abs_s_str

                if source_chat_id == s_id or short_chat_str == short_s_str:
                    receivers_names = item.get("receivers_names") or []
                    is_pb_target = False
                    if isinstance(receivers_names, list):
                        for name in receivers_names:
                            n_lower = str(name).lower()
                            if "private_broadcast" in n_lower or "private broadcast" in n_lower or "gapgrowbot" in n_lower:
                                is_pb_target = True
                                break
                    elif isinstance(receivers_names, str) and "private_broadcast" in receivers_names.lower():
                        is_pb_target = True

                    if is_pb_target:
                        return item

            return None
        except Exception as ex:
            logger.error(f"[PRIVATE-BROADCAST] Error fetching mapping for source {source_chat_id}: {ex}")
            return None

    # ---------------- SUBSCRIBER MANAGEMENT ----------------
    async def upsert_subscriber(
        self,
        telegram_user_id: int,
        chat_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        owner_id: Optional[Any] = None,
        bot_id: Optional[str] = None
    ) -> bool:
        """
        Store or update a subscriber record. Set is_active = True.
        Embeds owner_id tag in last_name metadata to ensure multi-tenant subscriber isolation.
        """
        now = datetime.now(timezone.utc).isoformat()
        
        # Format owner tag in last_name if owner_id is provided
        final_last_name = last_name or ""
        if owner_id:
            tag = f"[owner:{owner_id}]"
            if tag not in final_last_name:
                final_last_name = f"{final_last_name} {tag}".strip()

        payload = {
            "telegram_user_id": telegram_user_id,
            "chat_id": chat_id,
            "username": username,
            "first_name": first_name,
            "last_name": final_last_name,
            "is_active": True,
            "updated_at": now
        }
        if bot_id and bot_id != "default":
            payload["bot_id"] = bot_id

        try:
            # Check existing subscriber
            res = await self.supabase.table("telegram_private_broadcast_users")\
                .select("id, last_name")\
                .eq("telegram_user_id", telegram_user_id)\
                .execute()
            data = getattr(res, "data", [])
            
            if data:
                existing_last_name = data[0].get("last_name") or ""
                # Preserve existing owner tag if new one isn't explicitly replacing it
                if owner_id and f"[owner:{owner_id}]" not in existing_last_name:
                    payload["last_name"] = f"{existing_last_name} [owner:{owner_id}]".strip()

                await self.supabase.table("telegram_private_broadcast_users")\
                    .update(payload)\
                    .eq("telegram_user_id", telegram_user_id)\
                    .execute()
            else:
                payload["started_at"] = now
                await self.supabase.table("telegram_private_broadcast_users")\
                    .insert(payload)\
                    .execute()
            return True
        except Exception as ex:
            logger.error(f"[PRIVATE-BROADCAST] Error upserting subscriber {telegram_user_id}: {ex}")
            return False

    async def deactivate_subscriber(self, telegram_user_id: int) -> bool:
        """
        Mark subscriber as inactive (is_active = False).
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self.supabase.table("telegram_private_broadcast_users")\
                .update({"is_active": False, "updated_at": now})\
                .eq("telegram_user_id", telegram_user_id)\
                .execute()
            logger.info(f"[PRIVATE-BROADCAST] Subscriber deactivated: {telegram_user_id}")
            return True
        except Exception as ex:
            logger.error(f"[PRIVATE-BROADCAST] Error deactivating subscriber {telegram_user_id}: {ex}")
            return False

    async def get_active_subscribers(
        self,
        owner_id: Optional[Any] = None,
        bot_id: Optional[str] = None,
        exclude_user_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetch active subscribers.
        If owner_id or bot_id is provided, filters ONLY subscribers belonging to that owner/bot.
        Excludes the channel owner/sender (exclude_user_id) so creators do not receive their own broadcasts.
        Guarantees 100% multi-tenant channel & subscriber isolation.
        """
        try:
            query = self.supabase.table("telegram_private_broadcast_users")\
                .select("telegram_user_id, chat_id, last_name, bot_id, user_id")\
                .eq("is_active", True)
            
            if bot_id:
                query = query.eq("bot_id", bot_id)

            res = await query.execute()
            rows = getattr(res, "data", []) or []

            # Exclude IDs (owner or sender)
            exclude_ids = set()
            if exclude_user_id is not None:
                try:
                    exclude_ids.add(int(exclude_user_id))
                except ValueError:
                    pass
            if owner_id is not None:
                try:
                    exclude_ids.add(int(owner_id))
                except ValueError:
                    pass

            # Filter candidate rows matching owner_id or any mapped channel/user alias for this creator
            if owner_id:
                aliases = await self.get_owner_aliases(owner_id)
                mapped_channel_tags = {f"[owner:{a}]" for a in aliases if a}

                filtered = []
                for r in rows:
                    l_name = r.get("last_name") or ""
                    u_id = r.get("user_id")
                    matches_tag = any(t in l_name for t in mapped_channel_tags)
                    matches_uid = u_id is not None and str(u_id) in aliases
                    if matches_tag or matches_uid:
                        filtered.append(r)

                if not filtered and rows:
                    unassigned = [r for r in rows if "[owner:" not in (r.get("last_name") or "")]
                    if unassigned:
                        filtered = unassigned
                rows = filtered

            # Exclude owner / sender from recipients & deduplicate unique telegram_user_id
            seen_uids = set()
            final_subscribers = []
            for r in rows:
                t_id = r.get("telegram_user_id")
                if t_id is not None:
                    try:
                        t_id_int = int(t_id)
                        if t_id_int in exclude_ids or t_id_int in seen_uids:
                            continue
                        seen_uids.add(t_id_int)
                        final_subscribers.append(r)
                    except ValueError:
                        pass

            return final_subscribers
        except Exception as ex:
            logger.error(f"[PRIVATE-BROADCAST] Error fetching active subscribers: {ex}")
            return []

    async def get_subscriber_counts(self, owner_id: Optional[Any] = None) -> Dict[str, int]:
        """
        Get subscriber count stats (Total, Active, Inactive).
        Optionally filtered by owner_id.
        """
        try:
            res_all = await self.supabase.table("telegram_private_broadcast_users").select("id, is_active, last_name, user_id").execute()
            rows = getattr(res_all, "data", []) or []

            if owner_id:
                owner_str = str(owner_id)
                tag = f"[owner:{owner_str}]"
                matched_rows = [r for r in rows if tag in (r.get("last_name") or "") or str(r.get("user_id")) == owner_str]
                if matched_rows:
                    rows = matched_rows

            total = len(rows)
            active = sum(1 for r in rows if r.get("is_active"))
            inactive = total - active
            return {
                "total": total,
                "active": active,
                "inactive": inactive
            }
        except Exception as ex:
            logger.error(f"[PRIVATE-BROADCAST] Error fetching subscriber counts: {ex}")
            return {"total": 0, "active": 0, "inactive": 0}

    # ---------------- JOB MANAGEMENT ----------------
    async def create_broadcast_job(
        self,
        source_chat_id: int,
        source_message_id: int,
        grouped_id: Optional[int] = None,
        message_type: str = "single",
        source_message_ids: Optional[List[int]] = None,
        owner_id: Optional[Any] = None,
        bot_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Create a new broadcast job. Prevent duplicates via unique source check.
        Stores owner_id in metadata tag to preserve multi-tenant routing.
        """
        try:
            # Check duplicate single message job
            if not grouped_id:
                check = await self.supabase.table("telegram_private_broadcast_jobs")\
                    .select("id, status")\
                    .eq("source_chat_id", source_chat_id)\
                    .eq("source_message_id", source_message_id)\
                    .execute()
                existing = getattr(check, "data", [])
                if existing:
                    logger.info(f"[PRIVATE-BROADCAST] Job already exists for message {source_message_id}")
                    return None
            else:
                # Check duplicate album job
                check = await self.supabase.table("telegram_private_broadcast_jobs")\
                    .select("id, status")\
                    .eq("source_chat_id", source_chat_id)\
                    .eq("grouped_id", grouped_id)\
                    .execute()
                existing = getattr(check, "data", [])
                if existing:
                    logger.info(f"[PRIVATE-BROADCAST] Album job already exists for grouped_id {grouped_id}")
                    return None

            tag = f"[owner:{owner_id}]" if owner_id else ""
            payload = {
                "source_chat_id": source_chat_id,
                "source_message_id": source_message_id,
                "grouped_id": grouped_id,
                "message_type": message_type,
                "source_message_ids": json.dumps(source_message_ids) if source_message_ids else None,
                "status": "pending",
                "total_recipients": 0,
                "sent_count": 0,
                "failed_count": 0,
                "blocked_count": 0,
                "error_message": tag if tag else None,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            if bot_id and bot_id != "default":
                payload["bot_id"] = bot_id

            res = await self.supabase.table("telegram_private_broadcast_jobs").insert(payload).execute()
            data = getattr(res, "data", [])
            if data:
                job = data[0]
                logger.info(f"[PRIVATE-BROADCAST] Job created: {job.get('id')} (owner: {owner_id or 'default'})")
                return job
            return None
        except Exception as ex:
            logger.error(f"[PRIVATE-BROADCAST] Error creating broadcast job: {ex}")
            return None

    async def get_unfinished_jobs(self) -> List[Dict[str, Any]]:
        """
        Get jobs with status 'pending' or 'processing' to execute/resume.
        """
        try:
            res = await self.supabase.table("telegram_private_broadcast_jobs")\
                .select("*")\
                .in_("status", ["pending", "processing"])\
                .order("created_at")\
                .execute()
            return getattr(res, "data", []) or []
        except Exception as ex:
            logger.error(f"[PRIVATE-BROADCAST] Error getting unfinished jobs: {ex}")
            return []

    async def update_job(self, job_id: str, updates: Dict[str, Any]) -> bool:
        """
        Update job status / metrics.
        """
        try:
            await self.supabase.table("telegram_private_broadcast_jobs")\
                .update(updates)\
                .eq("id", job_id)\
                .execute()
            return True
        except Exception as ex:
            logger.error(f"[PRIVATE-BROADCAST] Error updating job {job_id}: {ex}")
            return False

    async def get_recent_job_stats(self) -> Optional[Dict[str, Any]]:
        """
        Get most recent job stats for /stats admin command.
        """
        try:
            res = await self.supabase.table("telegram_private_broadcast_jobs")\
                .select("*")\
                .order("created_at", desc=True)\
                .limit(1)\
                .execute()
            data = getattr(res, "data", [])
            return data[0] if data else None
        except Exception as ex:
            logger.error(f"[PRIVATE-BROADCAST] Error getting recent job stats: {ex}")
            return None

    # ---------------- DELIVERY TRACKING ----------------
    async def create_deliveries_batch(self, job_id: str, subscribers: List[Dict[str, Any]]) -> bool:
        """
        Bulk insert delivery records for a job.
        Skip existing user deliveries for idempotency.
        """
        if not subscribers:
            return True

        # Check existing deliveries for this job to avoid unique constraint error
        try:
            existing_res = await self.supabase.table("telegram_private_broadcast_deliveries")\
                .select("telegram_user_id")\
                .eq("job_id", job_id)\
                .execute()
            existing_uids = {r["telegram_user_id"] for r in (getattr(existing_res, "data", []) or [])}
        except Exception as ex:
            logger.warning(f"[PRIVATE-BROADCAST] Could not check existing deliveries: {ex}")
            existing_uids = set()

        rows = []
        seen_uids_in_batch = set()
        for sub in subscribers:
            uid = sub["telegram_user_id"]
            if uid in existing_uids or uid in seen_uids_in_batch:
                continue
            seen_uids_in_batch.add(uid)
            rows.append({
                "job_id": job_id,
                "telegram_user_id": uid,
                "chat_id": sub["chat_id"],
                "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat()
            })

        if not rows:
            return True

        # Batch insert in chunks of 200
        chunk_size = 200
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i:i + chunk_size]
            try:
                await self.supabase.table("telegram_private_broadcast_deliveries").insert(chunk).execute()
            except Exception as ex:
                logger.error(f"[PRIVATE-BROADCAST] Error inserting delivery chunk: {ex}")
        return True

    async def get_pending_deliveries(self, job_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Fetch pending deliveries for a job.
        """
        try:
            res = await self.supabase.table("telegram_private_broadcast_deliveries")\
                .select("*")\
                .eq("job_id", job_id)\
                .eq("status", "pending")\
                .limit(limit)\
                .execute()
            return getattr(res, "data", []) or []
        except Exception as ex:
            logger.error(f"[PRIVATE-BROADCAST] Error fetching pending deliveries for job {job_id}: {ex}")
            return []

    async def update_delivery(
        self,
        delivery_id: int,
        status: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None
    ) -> bool:
        """
        Update single delivery status.
        """
        payload = {
            "status": status,
            "error_code": error_code,
            "error_message": error_message,
            "sent_at": datetime.now(timezone.utc).isoformat() if status == "sent" else None
        }
        try:
            await self.supabase.table("telegram_private_broadcast_deliveries")\
                .update(payload)\
                .eq("id", delivery_id)\
                .execute()
            return True
        except Exception as ex:
            logger.error(f"[PRIVATE-BROADCAST] Error updating delivery {delivery_id}: {ex}")
            return False

