"""Durable forwarding references and restart replay; no message bodies stored."""
import asyncio
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from types import SimpleNamespace

from telethon import errors

_event = ContextVar("autoforward_event", default=None)
_receipt = ContextVar("autoforward_receipt", default=None)


def request_random_id():
    receipt = _receipt.get()
    return receipt["random_id"] if receipt else None


def lease_params(client):
    manager = client.owner_manager
    state = next(s for s in manager.clients.values() if s["client"] is client)
    return {"p_credential": state["row"]["credential"]["id"], "p_worker": manager.worker_id,
            "p_fence": state["row"]["lease"]["fence"]}


async def rpc(client, name, params):
    client.assert_owned()
    async with asyncio.timeout(8):
        return await client.owner_manager.store.rpc(name, {**lease_params(client), **params})


@asynccontextmanager
async def forwarding_event(client, source_id, message_id, targets):
    await rpc(client, "tg_autoforward_event_stage", {"p_source": int(source_id), "p_message": message_id,
                                                   "p_targets": list(dict.fromkeys(map(int, targets)))})
    event = {"p_source": int(source_id), "p_message": message_id, "parts": {}}
    token = _event.set(event)
    try:
        yield
    finally:
        _event.reset(token)
        # Empty/fully filtered messages intentionally have no outbound send.
        # Leave attempted failures pending, but retire untouched target jobs.
        await rpc(client, "tg_autoforward_delivery_abandon", {"p_source": int(source_id), "p_message": message_id,
                                                             "p_keep": list(event["parts"])})


async def deliver(client, target, operation):
    event = _event.get()
    if not event or _receipt.get():
        return await operation()
    target = int(target)
    part = event["parts"].get(target, 0)
    event["parts"][target] = part + 1
    params = {"p_source": event["p_source"], "p_message": event["p_message"], "p_target": target, "p_part": part}
    if part:
        await rpc(client, "tg_autoforward_event_stage", {"p_source": event["p_source"], "p_message": event["p_message"], "p_targets": [target], "p_part": part})
    receipt = await rpc(client, "tg_autoforward_delivery_begin", params)
    if not receipt:
        return None
    token = _receipt.set(receipt)
    try:
        try:
            async with asyncio.timeout(min(20, client.lease_deadline - time.monotonic())):
                result = await operation()
        except errors.RandomIdDuplicateError:
            result = None  # Telegram confirms this persisted message ID was used.
        except Exception as error:
            await rpc(client, "tg_autoforward_delivery_end", {**params, "p_ok": False,
                "p_wait": error.seconds if isinstance(error, errors.FloodWaitError) else 0})
            raise
        await rpc(client, "tg_autoforward_delivery_end", {**params, "p_ok": True})
        return result
    finally:
        _receipt.reset(token)


async def _resolve_peer(client, peer_id):
    try:
        return await client.get_input_entity(peer_id)
    except Exception:
        try:
            return await client.get_entity(peer_id)
        except Exception:
            try:
                await client.get_dialogs(limit=100)
                return await client.get_input_entity(peer_id)
            except Exception:
                return peer_id


async def initialize_sources(client, mapping):
    """Initialize cursors before handlers attach so failed staging can be replayed."""
    manager = client.owner_manager
    identity = lease_params(client)["p_credential"]
    client.forward_baselines = getattr(client, "forward_baselines", {})
    for source in mapping:
        if await manager.store.source_cursor(identity, source) is None:
            replay_since = await manager.store.source_replay_since(identity, source)
            peer = await _resolve_peer(client, source)
            latest = await client.get_messages(peer, limit=1,
                **({"offset_date": datetime.fromisoformat(replay_since.replace("Z", "+00:00"))} if replay_since else {}))
            baseline = latest[0].id if latest else 0
            await rpc(client, "tg_autoforward_source_cursor", {"p_source": source, "p_message": baseline})
        client.forward_baselines[source] = await manager.store.source_cursor(identity, source)


async def reconcile_once(client, mapping, handler):
    """Replay queued references first, then scan sources beyond durable cursors."""
    manager = client.owner_manager
    identity = lease_params(client)["p_credential"]
    jobs = await manager.store.pending_deliveries(identity)
    seen = set()
    for job in jobs:
        source, message_id = int(job["source_id"]), int(job["message_id"])
        if (source, message_id) in seen:
            continue
        seen.add((source, message_id))
        keep = list(mapping.get(source, []))
        relay = getattr(client, "relay_target", None)
        if keep and relay:
            keep.append(relay)
        await rpc(client, "tg_autoforward_delivery_abandon", {"p_source": source, "p_message": message_id, "p_keep": keep})
        if not keep:
            continue
        peer = await _resolve_peer(client, source)
        message = await client.get_messages(peer, ids=message_id)
        if message:
            await handler(SimpleNamespace(chat_id=source, sender_id=source, message=message, recovery=True))
        else:
            await rpc(client, "tg_autoforward_delivery_abandon", {"p_source": source, "p_message": message_id, "p_keep": []})
    await initialize_sources(client, mapping)
    for source in mapping:
        cursor = await manager.store.source_cursor(identity, source)
        peer = await _resolve_peer(client, source)
        async for message in client.iter_messages(peer, min_id=int(cursor), reverse=True, limit=100):
            if not getattr(message, "message", None) and not getattr(message, "media", None):
                await rpc(client, "tg_autoforward_source_cursor", {"p_source": source, "p_message": message.id, "p_advance": True})
                continue
            await handler(SimpleNamespace(chat_id=source, sender_id=source, message=message, recovery=True))
            # The handler stages every target before sending. Pending sends are
            # durable even if a target fails; cursor progress cannot erase them.
            staged = await manager.store.event_staged(identity, source, message.id)
            if not staged:
                break
            await rpc(client, "tg_autoforward_source_cursor", {"p_source": source, "p_message": message.id, "p_advance": True})


async def recovery_loop(client, mapping_provider, handler):
    while client.is_connected():
        try:
            client.assert_owned()
            await reconcile_once(client, mapping_provider(), handler)
        except asyncio.CancelledError:
            return
        except Exception:
            # Retry transient read/send errors. Lease heartbeat fails closed.
            client.assert_owned()
        await asyncio.sleep(30)
