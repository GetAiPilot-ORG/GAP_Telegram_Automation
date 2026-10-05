"""AutoForward durable login, fenced session ownership, and checkpoint lifecycle."""
import asyncio
import time
from uuid import uuid4

from telethon import TelegramClient
from telethon.sessions import StringSession

from .database_session import DatabaseSession, decrypt_snapshot, encrypt_snapshot
from .login import DurableLogin, LoginError, SupabaseLoginStore, credential_context


class AutoForwardStore(SupabaseLoginStore):
    def __init__(self, client):
        super().__init__(client, "autoforward")

    async def telegram_credential(self, telegram_id):
        response = await self._execute(lambda: self.client.table("tg_session_credentials").select("*")
            .eq("telegram_owner_id", telegram_id).eq("purpose", "autoforward").eq("principal_kind", "user")
            .maybe_single().execute())
        return response.data if response else None

    async def checkpoint(self, credential_id):
        response = await self._execute(lambda: self.client.table("tg_autoforward_checkpoints").select("*")
            .eq("credential_id", credential_id).maybe_single().execute())
        return response.data if response else None

    async def pending_deliveries(self, credential_id):
        result = await self._execute(lambda: self.client.table("tg_autoforward_deliveries").select("source_id,message_id")
            .eq("credential_id", credential_id).in_("state", ["pending", "sending"]).order("message_id").limit(100).execute())
        return result.data or []

    async def source_cursor(self, credential_id, source):
        result = await self._execute(lambda: self.client.table("tg_autoforward_sources").select("last_message_id,needs_reset")
            .eq("credential_id", credential_id).eq("source_id", source).maybe_single().execute())
        return result.data["last_message_id"] if result and result.data and not result.data["needs_reset"] else None

    async def source_replay_since(self, credential_id, source):
        result = await self._execute(lambda: self.client.table("tg_autoforward_sources").select("replay_since")
            .eq("credential_id", credential_id).eq("source_id", source).maybe_single().execute())
        return result.data.get("replay_since") if result and result.data else None

    async def event_staged(self, credential_id, source, message):
        result = await self._execute(lambda: self.client.table("tg_autoforward_deliveries").select("target_id")
            .eq("credential_id", credential_id).eq("source_id", source).eq("message_id", message).limit(1).execute())
        return bool(result.data)


class AutoForwardLogin(DurableLogin):
    def __init__(self, store, cipher, api_id, api_hash, client_factory=None):
        super().__init__(store, cipher, client_factory or (lambda value: TelegramClient(
            StringSession(value), api_id, api_hash, receive_updates=False,
            flood_sleep_threshold=0, connection_retries=1)), purpose="autoforward")

    async def resume(self, owner, attempt_id):
        try:
            return await super().resume(owner, attempt_id)
        except LoginError as error:
            if error.code == "attempt_stale":
                attempt = await self.store.attempt(owner, attempt_id)
                row = await self.store.credential(owner)
                if attempt and attempt["step"] == "completed" and row and row["state"] == "active":
                    return {"step": "done", "attempt_id": attempt_id}
            raise

    async def logout(self, owner):
        row = await self.store.rpc("tg_submanager_revoke", {"p_owner": owner, "p_done": False})
        if not row or row["state"] == "revoked":
            return {"status": "success"}
        session = None
        try:
            async with asyncio.timeout(self.timeout):
                saved = await self.store.checkpoint(row["id"])
                if saved:
                    session = DatabaseSession(snapshot=decrypt_snapshot(self.encryption, saved["encrypted_snapshot"], row))
                    value = StringSession.save(session)
                else:
                    value = self.encryption.decrypt(row["encrypted_session"], credential_context(row))
                client = self.client_factory(value)
                try:
                    await client.connect()
                    if await client.is_user_authorized():
                        me = await client.get_me()
                        if me.id != row["telegram_owner_id"] or not await client.log_out():
                            raise LoginError("revocation_pending", "Logout is pending. Please retry.", 503)
                finally:
                    await self._disconnect(client)
                await self.store.rpc("tg_submanager_revoke", {"p_owner": owner, "p_done": True, "p_version": row["version"]})
                return {"status": "success"}
        except Exception:
            raise LoginError("revocation_pending", "Logout is pending. Please retry.", 503) from None
        finally:
            if session:
                session.dispose()


class LeasedClient(TelegramClient):
    def __init__(self, *args, **kwargs):
        self.lease_deadline = 0
        self.owner_manager = None
        super().__init__(*args, **kwargs)

    def assert_owned(self):
        if time.monotonic() >= self.lease_deadline:
            raise LoginError("lease_lost", "Forwarding connection ownership expired.", 409)

    async def _call(self, *args, **kwargs):
        self.assert_owned()
        from .forward_delivery import request_random_id
        random_id = request_random_id()
        request = args[1] if len(args) > 1 else kwargs.get("request")
        if random_id is not None and hasattr(request, "random_id"):
            request.random_id = random_id
        # Bound internal Telegram retries to the current ownership window.
        async with asyncio.timeout(min(20, self.lease_deadline - time.monotonic())):
            return await super()._call(*args, **kwargs)

    async def send_message(self, entity, *args, **kwargs):
        from .forward_delivery import deliver
        parent = super().send_message
        return await deliver(self, entity, lambda: parent(entity, *args, **kwargs))

    async def send_file(self, entity, *args, **kwargs):
        from .forward_delivery import deliver
        parent = super().send_file
        return await deliver(self, entity, lambda: parent(entity, *args, **kwargs))

    async def disconnect(self):
        if self.owner_manager is not None:
            return await self.owner_manager.close(self)
        return await super().disconnect()


class AutoForwardClients:
    def __init__(self, store, cipher, api_id, api_hash, client_factory=LeasedClient, on_loss=None):
        self.store, self.cipher = store, cipher
        self.api_id, self.api_hash = api_id, api_hash
        self.factory, self.on_loss = client_factory, on_loss
        self.worker_id = str(uuid4())
        self.clients, self.locks = {}, {}

    async def get(self, telegram_id):
        async with self.locks.setdefault(telegram_id, asyncio.Lock()):
            cached = self.clients.get(telegram_id)
            if cached and cached["closing"]:
                raise LoginError("lease_busy", "Forwarding connection is stopping. Please retry.", 409)
            if cached and cached["client"].is_connected():
                cached["client"].assert_owned()
                return cached["client"]
            if cached:
                await self.close(cached["client"])
            async with asyncio.timeout(8):
                row = await self.store.rpc("tg_autoforward_lease_claim", {"p_telegram": telegram_id, "p_worker": self.worker_id})
            credential = row["credential"]
            saved = row.get("checkpoint")
            session = DatabaseSession(snapshot=decrypt_snapshot(self.cipher, saved["encrypted_snapshot"], credential)) if saved else DatabaseSession(
                self.cipher.decrypt(credential["encrypted_session"], credential_context(credential)))
            client = self.factory(session, self.api_id, self.api_hash, catch_up=False, connection_retries=1,
                                  flood_sleep_threshold=0)
            client.lease_deadline = time.monotonic() + 40
            client.owner_manager = self
            state = {"client": client, "row": row, "closing": False, "task": None, "io_lock": asyncio.Lock()}
            self.clients[telegram_id] = state
            try:
                async with asyncio.timeout(20):
                    await client.connect()
                    if not await client.is_user_authorized() or (await client.get_me()).id != telegram_id:
                        raise LoginError("identity_mismatch", "Saved Telegram account requires attention.", 409)
                await self._checkpoint(state)
                state["task"] = asyncio.create_task(self._heartbeat(telegram_id, state))
                return client
            except BaseException:
                await self.close(client, persist=False)
                raise

    async def _checkpoint(self, state):
        async with state["io_lock"]:
            client, row = state["client"], state["row"]
            client.assert_owned()
            payload = encrypt_snapshot(self.cipher, client.session.snapshot(), row["credential"])
            async with asyncio.timeout(8):
                await self.store.rpc("tg_autoforward_checkpoint", {"p_credential": row["credential"]["id"],
                    "p_worker": self.worker_id, "p_fence": row["lease"]["fence"], "p_snapshot": payload,
                    "p_key": self.cipher.active_key_version})
            client.lease_deadline = time.monotonic() + 40

    async def _heartbeat(self, telegram_id, state):
        try:
            while True:
                await asyncio.sleep(10)
                await self._checkpoint(state)
        except asyncio.CancelledError:
            return
        except Exception:
            state["client"].lease_deadline = 0
            await self.close(state["client"], persist=False)
            if self.on_loss:
                self.on_loss(telegram_id)

    async def close(self, client, persist=True):
        found = next(((uid, state) for uid, state in self.clients.items() if state["client"] is client), None)
        if not found:
            return
        uid, state = found
        if state["closing"]:
            return
        state["closing"] = True
        recovery = getattr(client, "recovery_task", None)
        if recovery and recovery is not asyncio.current_task():
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
        task = state["task"]
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Keep ownership until network disconnect and final checkpoint finish.
        try:
            await asyncio.wait_for(TelegramClient.disconnect(client), 5)
            if persist:
                await self._checkpoint(state)
            row = state["row"]
            async with asyncio.timeout(8):
                await self.store.rpc("tg_autoforward_lease_release", {"p_credential": row["credential"]["id"],
                    "p_worker": self.worker_id, "p_fence": row["lease"]["fence"]})
        except Exception:
            pass  # Leave lease to expire; never release a possibly live connection.
        finally:
            client.lease_deadline = 0
            self.clients.pop(uid, None)
            if not client.is_connected():
                client.session.dispose()
            else:
                raise SystemExit("AutoForward cannot disconnect a lost session lease; stopping worker")

    async def shutdown(self):
        await asyncio.gather(*[self.close(s["client"]) for s in list(self.clients.values())])
