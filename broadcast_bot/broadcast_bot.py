import os
import asyncio
import logging
import httpx
from dotenv import load_dotenv
from supabase import create_async_client, AsyncClient
from telethon import TelegramClient, events, Button

# ---- Logging Setup ----
# Get script directory for absolute logging path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

# Set root level to WARNING to avoid noise from libraries
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.WARNING,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "broadcast_bot.log"))
    ]
)
logger = logging.getLogger("BroadcastBot")
logger.setLevel(logging.INFO)

# Silence specific libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

load_dotenv(os.path.join(BASE_DIR, ".env"))
load_dotenv()

SUPABASE_URL = os.environ.get("VITE_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("VITE_SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_KEY")
API_ID = int(os.environ.get("TELEGRAM_API_ID", "12345678"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "dummyhash")
BOT_TOKEN = os.environ.get("BROADCAST_BOT_TOKEN")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL and Key must be defined in environment variables")

# Initialize globally as None, then await in main()
supabase: AsyncClient = None

# ---- Supabase Optimization Removed ----
# AsyncClient handles concurrency natively.

# {user_id: {'step': 'selecting_channel', 'channel_id': '...', 'channel_name': '...', 'message_data': {...}}}
user_states = {}

async def get_owner_user_ids(tg_user_id):
    if not supabase: return []
    user_ids = set()
    for val in [tg_user_id, str(tg_user_id)]:
        try:
            r1 = await supabase.table('profiles').select('id').eq('telegram_user_id', val).execute()
            for r in getattr(r1, 'data', []) or []:
                if r.get('id'): user_ids.add(r['id'])
        except Exception as e:
            logger.warning(f"Error querying profiles for tg_user_id {val}: {e}")
        try:
            r2 = await supabase.table('app_user_subscriptions').select('user_id').eq('telegram_user_id', val).execute()
            for r in getattr(r2, 'data', []) or []:
                if r.get('user_id'): user_ids.add(r['user_id'])
        except Exception as e:
            logger.warning(f"Error querying app_user_subscriptions for tg_user_id {val}: {e}")
    return list(user_ids)

async def get_owner_data(tg_user_id):
    if not supabase: return None
    user_ids = await get_owner_user_ids(tg_user_id)
    if not user_ids:
        return None
    # Pick the user_id that owns tracker bots or active records
    for uid in user_ids:
        try:
            t_res = await supabase.table('tg_tracker').select('id').eq('user_id', uid).limit(1).execute()
            if getattr(t_res, 'data', []):
                return {'id': uid, 'all_ids': user_ids}
        except Exception:
            pass
    return {'id': user_ids[0], 'all_ids': user_ids}

async def get_owner_channels(user_id_or_tg_id):
    if not supabase: return []
    
    # Resolve all associated user IDs
    user_ids = set()
    if isinstance(user_id_or_tg_id, int) or (isinstance(user_id_or_tg_id, str) and user_id_or_tg_id.isdigit()):
        user_ids = set(await get_owner_user_ids(user_id_or_tg_id))
    else:
        user_ids.add(str(user_id_or_tg_id))
        # Also find any other user IDs tied to same Telegram user ID
        try:
            p_res = await supabase.table('profiles').select('telegram_user_id').eq('id', user_id_or_tg_id).execute()
            p_data = getattr(p_res, 'data', []) or []
            if p_data and p_data[0].get('telegram_user_id'):
                user_ids.update(await get_owner_user_ids(p_data[0]['telegram_user_id']))
        except Exception:
            pass

    if not user_ids:
        return []

    user_ids_list = list(user_ids)
    channels = []
    seen_clean_ids = set()

    # 1. Fetch tracker bots for these users
    bots_res = await supabase.table('tg_tracker').select('id, bot_name, bot_token, channel_id, channel_name, status').in_('user_id', user_ids_list).execute()
    bots = getattr(bots_res, 'data', []) or []
    bot_ids = [b['id'] for b in bots if b.get('id')]

    # 2. Fetch from tg_bot_channel_mappings (Tracker Mappings)
    if bot_ids:
        try:
            mappings_res = await supabase.table('tg_bot_channel_mappings').select('id, bot_id, channel_id, channel_name, status').in_('bot_id', bot_ids).execute()
            for m in getattr(mappings_res, 'data', []) or []:
                cid = m.get('channel_id')
                cname = m.get('channel_name')
                st = (m.get('status') or '').lower()
                if cid and st not in ['deleted', 'removed']:
                    clean = str(cid).replace('-100', '').replace('-', '')
                    if clean and clean not in seen_clean_ids:
                        seen_clean_ids.add(clean)
                        channels.append({
                            'channel_id': cid,
                            'clean_id': clean,
                            'channel_name': cname or f"Channel {clean}",
                            'source': 'tracker_mapping',
                            'bot_id': m.get('bot_id'),
                            'mapping_id': m.get('id')
                        })
        except Exception as e:
            logger.warning(f"Error fetching tg_bot_channel_mappings: {e}")

    # 3. Direct channels on tg_tracker
    for b in bots:
        cid = b.get('channel_id')
        cname = b.get('channel_name')
        if cid and cname:  # Only if a valid channel name exists
            clean = str(cid).replace('-100', '').replace('-', '')
            if clean and clean not in seen_clean_ids:
                seen_clean_ids.add(clean)
                channels.append({
                    'channel_id': cid,
                    'clean_id': clean,
                    'channel_name': cname,
                    'source': 'tracker_direct',
                    'bot_id': b.get('id')
                })

    # 4. Fetch from tg_communities (Telesub / Subscription Channels)
    try:
        comm_res = await supabase.table('tg_communities').select('id, telegram_chat_id, title').in_('user_id', user_ids_list).execute()
        for c in getattr(comm_res, 'data', []) or []:
            cid = c.get('telegram_chat_id')
            cname = c.get('title')
            if cid:
                clean = str(cid).replace('-100', '').replace('-', '')
                if clean and clean not in seen_clean_ids:
                    seen_clean_ids.add(clean)
                    channels.append({
                        'channel_id': cid,
                        'clean_id': clean,
                        'channel_name': cname or f"Community {clean}",
                        'source': 'community',
                        'community_id': c.get('id')
                    })
    except Exception as e:
        logger.warning(f"Error fetching tg_communities: {e}")

    # 5. Fetch from tg_autopost_channels
    try:
        ap_res = await supabase.table('tg_autopost_channels').select('channel_id, channel_title, bot_id').in_('user_id', user_ids_list).execute()
        for a in getattr(ap_res, 'data', []) or []:
            cid = a.get('channel_id')
            cname = a.get('channel_title')
            if cid:
                clean = str(cid).replace('-100', '').replace('-', '')
                if clean and clean not in seen_clean_ids:
                    seen_clean_ids.add(clean)
                    channels.append({
                        'channel_id': cid,
                        'clean_id': clean,
                        'channel_name': cname or f"Channel {clean}",
                        'source': 'autopost',
                        'bot_id': a.get('bot_id')
                    })
    except Exception as e:
        logger.warning(f"Error fetching tg_autopost_channels: {e}")

    return channels

async def main():
    global supabase
    if not BOT_TOKEN:
        logger.error("BROADCAST_BOT_TOKEN not found in .env")
        return
    
    # Initialize the Async client
    supabase = await create_async_client(SUPABASE_URL, SUPABASE_KEY)

    # Ensure sessions directory exists relative to the script
    base_dir = os.path.dirname(os.path.abspath(__file__))
    sessions_dir = os.path.join(base_dir, "sessions")
    if not os.path.exists(sessions_dir):
        os.makedirs(sessions_dir)

    session_path = os.path.join(sessions_dir, "broadcast_master")
    
    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    logger.info("Broadcast Master Bot (@Gapgrowbot) started!")

    @client.on(events.NewMessage)
    async def global_message_handler(event):
        sender_id = event.sender_id
        if not event.is_private:
            return

        text = event.text or ""
        
        # 1. Handle commands always
        if text.startswith('/start'):
            payload = None
            if ' ' in text:
                payload = text.split(' ', 1)[1]
            
            sender = await event.get_sender()
            logger.info(f"Handling /start for {sender.id} with payload: {payload}")

            if payload and payload.lower() != "true":
                logger.info(f"Attempting to link account for UUID: {payload} with TG ID: {sender.id}")
                try:
                    await supabase.table('profiles').update({'telegram_user_id': sender.id}).eq('id', payload).execute()
                    await supabase.table('app_user_subscriptions').update({'telegram_user_id': sender.id}).eq('user_id', payload).execute()
                    await event.respond("✅ **Telegram Account Connected Successfully!**")
                    return
                except Exception as e:
                    logger.error(f"Error linking account: {e}")
                    await event.respond(f"❌ Failed to link account: {str(e)}")
                    return

            owner = await get_owner_data(sender.id)
            if not owner:
                await event.respond("🚀 **Welcome!** Please connect your account in the dashboard first.")
                return

            await event.respond(
                "🚀 **Welcome to GAP Grow Broadcast Bot**\n\n"
                "Use /send to start building your broadcast."
            )
            return

        if text.startswith('/send'):
            sender = await event.get_sender()
            owner_data = await get_owner_data(sender.id)
            if not owner_data:
                await event.respond("Owner verification failed. Please connect your Telegram account from the dashboard.")
                return

            channels = await get_owner_channels(sender.id)
            if not channels:
                await event.respond("No active channels found in your connected bots or tracker. Please make sure your channel is mapped in the GAP dashboard.")
                return

            buttons = []
            available_channels = {}
            for ch in channels:
                clean = ch.get('clean_id') or str(ch.get('channel_id')).replace("-100", "").replace("-", "")
                available_channels[clean] = ch
                buttons.append([Button.inline(ch.get('channel_name') or "Unnamed Channel", data=f"selchan_{clean}")])

            user_states[sender.id] = {
                'step': 'selecting_channel',
                'available_channels': available_channels
            }
            await event.respond("Select the target channel audience:", buttons=buttons)
            return

        # 2. Handle state-based messages
        state = user_states.get(sender_id)
        if not state:
            return

        if state.get('step') == 'awaiting_message':
            media_path = None
            if event.media:
                media_dir = os.path.join(os.path.dirname(__file__), "broadcast_media")
                os.makedirs(media_dir, exist_ok=True)
                media_path = await event.download_media(file=os.path.join(media_dir, f"tmp_{event.id}"))
                logger.info(f"Downloaded media: {media_path}")

            state.update({
                'step': 'verifying',
                'original_msg': event.message,
                'media_path': media_path,
                'message_data': {
                    'text': event.text,
                    'media': event.media is not None,
                    'raw_text': event.message.message
                }
            })
            
            await event.respond("📝 **Preview of your broadcast message:**")
            preview_msg = await event.message.reply(
                f"Broadcast to audience of **{state['channel_name']}**?",
                buttons=[
                    [Button.inline("✅ Send Broadcast", data="confirm_send")],
                    [Button.inline("❌ Cancel", data="cancel_broadcast")]
                ]
            )
            state['preview_msg_id'] = preview_msg.id

    @client.on(events.CallbackQuery(data=lambda d: d.decode().startswith('selchan_')))
    async def channel_selection_handler(event):
        clean_id = event.data.decode().split('_', 1)[1]
        sender_id = event.sender_id
        state = user_states.get(sender_id, {})
        available = state.get('available_channels', {})
        ch_info = available.get(clean_id)
        
        if ch_info:
            channel_id = ch_info.get('channel_id')
            channel_name = ch_info.get('channel_name') or "Channel"
            bot_id = ch_info.get('bot_id')
            source = ch_info.get('source')
        else:
            channel_id = clean_id
            channel_name = "Channel"
            bot_id = None
            source = None
            try:
                res = await supabase.table('tg_bot_channel_mappings').select('channel_name, bot_id').in_('channel_id', [clean_id, f"-100{clean_id}"]).limit(1).execute()
                if res.data:
                    channel_name = res.data[0].get('channel_name') or "Channel"
                    bot_id = res.data[0].get('bot_id')
            except Exception:
                pass

        user_states[sender_id] = {
            'step': 'awaiting_message',
            'channel_id': channel_id,
            'clean_id': clean_id,
            'channel_name': channel_name,
            'bot_id': bot_id,
            'source': source,
            'available_channels': available
        }
        await event.edit(f"✅ **{channel_name}** selected. Send your message now.")

    @client.on(events.CallbackQuery(data='confirm_send'))
    async def confirm_handler(event):
        sender_id = event.sender_id
        state = user_states.get(sender_id)
        if not state or state.get('step') != 'verifying':
            await event.answer("Session expired or invalid state.")
            return

        try:
            channel_id = state.get('channel_id')
            clean_id = state.get('clean_id') or str(channel_id).replace("-100", "").replace("-", "")
            channel_name = state.get('channel_name') or "Channel"
            orig_msg = state.get('original_msg')
            media_path = state.get('media_path')
            raw_text = orig_msg.message if orig_msg else ""
            owner_data = await get_owner_data(sender_id)
            user_id = owner_data.get('id') if owner_data else None

            # 1. Update UI to indicate sending
            await event.edit(f"⏳ **Broadcasting message to {channel_name}...**")

            # 2. Record task in Supabase
            task_data = {
                'user_id': user_id,
                'channel_id': channel_id,
                'message_data': {
                    'text': orig_msg.text if orig_msg else "",
                    'has_media': bool(orig_msg.media) if orig_msg else False,
                    'raw_text': raw_text,
                    'media_path': media_path
                },
                'status': 'processing'
            }
            task_id = None
            try:
                task_res = await supabase.table('tg_broadcast_tasks').insert(task_data).execute()
                if task_res.data:
                    task_id = task_res.data[0]['id']
            except Exception as e:
                logger.warning(f"Error inserting task into DB: {e}")

            # 3. Post to Channel
            target_peer = int(f"-100{clean_id}")
            
            channel_sent = False
            channel_error = None

            # Attempt A: Send directly via @Gapgrowbot (Telethon client)
            try:
                if media_path and os.path.exists(media_path):
                    await client.send_message(target_peer, raw_text, file=media_path)
                else:
                    await client.send_message(target_peer, raw_text)
                channel_sent = True
                logger.info(f"Broadcast posted to {channel_name} via Gapgrowbot")
            except Exception as e:
                channel_error = str(e)
                logger.warning(f"Gapgrowbot could not post to {channel_name}: {e}")

            # Attempt B: Fallback via channel's mapped bot in tg_tracker
            if not channel_sent:
                try:
                    possible_cids = [str(channel_id), clean_id, f"-100{clean_id}"]
                    bot_tokens_to_try = []

                    # If bot_id was recorded during channel selection
                    if state.get('bot_id'):
                        b_res = await supabase.table('tg_tracker').select('bot_token').eq('id', state['bot_id']).execute()
                        for b in getattr(b_res, 'data', []) or []:
                            if b.get('bot_token'):
                                bot_tokens_to_try.append(b['bot_token'])

                    # Check mappings
                    m_res = await supabase.table('tg_bot_channel_mappings').select('bot_id').in_('channel_id', possible_cids).execute()
                    mappings = getattr(m_res, 'data', []) or []
                    for m in mappings:
                        b_res = await supabase.table('tg_tracker').select('bot_token').eq('id', m['bot_id']).execute()
                        for b in getattr(b_res, 'data', []) or []:
                            if b.get('bot_token') and b['bot_token'] not in bot_tokens_to_try:
                                bot_tokens_to_try.append(b['bot_token'])

                    # Check autopost bots if applicable
                    try:
                        ap_res = await supabase.table('tg_autopost_channels').select('bot_id').in_('channel_id', possible_cids).execute()
                        for ap in getattr(ap_res, 'data', []) or []:
                            if ap.get('bot_id'):
                                ab_res = await supabase.table('tg_autopost_bots').select('bot_token').eq('id', ap['bot_id']).execute()
                                for ab in getattr(ab_res, 'data', []) or []:
                                    if ab.get('bot_token') and ab['bot_token'] not in bot_tokens_to_try:
                                        bot_tokens_to_try.append(ab['bot_token'])
                    except Exception:
                        pass

                    for sub_token in bot_tokens_to_try:
                        async with httpx.AsyncClient(timeout=15.0) as http:
                            if media_path and os.path.exists(media_path):
                                with open(media_path, "rb") as f:
                                    resp = await http.post(
                                        f"https://api.telegram.org/bot{sub_token}/sendDocument",
                                        data={"chat_id": target_peer, "caption": raw_text},
                                        files={"document": f}
                                    )
                                    if resp.status_code == 200 and resp.json().get("ok"):
                                        channel_sent = True
                                        channel_error = None
                                        break
                            else:
                                resp = await http.post(
                                    f"https://api.telegram.org/bot{sub_token}/sendMessage",
                                    json={"chat_id": target_peer, "text": raw_text}
                                )
                                if resp.status_code == 200 and resp.json().get("ok"):
                                    channel_sent = True
                                    channel_error = None
                                    break
                except Exception as ex:
                    logger.warning(f"Fallback bot failed: {ex}")

            # 4. Also send DMs to tracked audience members who joined via bot invite links
            users_sent = 0
            users_total = 0
            try:
                possible_cids = [str(channel_id), clean_id, f"-100{clean_id}"]
                m_res = await supabase.table('tg_bot_channel_mappings').select('id, bot_id').in_('channel_id', possible_cids).execute()
                mappings = getattr(m_res, 'data', []) or []
                
                # Also include state bot_id if available
                all_mapping_ids = [m['id'] for m in mappings if m.get('id')]
                all_bot_ids = list(set([m['bot_id'] for m in mappings if m.get('bot_id')] + ([state.get('bot_id')] if state.get('bot_id') else [])))

                link_ids = []
                if all_mapping_ids:
                    l_res = await supabase.table('tg_bot_join_links').select('id').in_('channel_mapping_id', all_mapping_ids).execute()
                    link_ids.extend([l['id'] for l in getattr(l_res, 'data', []) if l.get('id')])
                
                if not link_ids and all_bot_ids:
                    l_res = await supabase.table('tg_bot_join_links').select('id').in_('bot_id', all_bot_ids).execute()
                    link_ids.extend([l['id'] for l in getattr(l_res, 'data', []) if l.get('id')])
                
                if link_ids:
                    u_res = await supabase.table('tg_bot_join_users').select('telegram_user_id').in_('link_id', link_ids).execute()
                    users = getattr(u_res, 'data', []) or []
                    users_total += len(users)
                    
                    for b_id in all_bot_ids:
                        b_res = await supabase.table('tg_tracker').select('bot_token').eq('id', b_id).execute()
                        b_data = getattr(b_res, 'data', []) or []
                        if b_data and b_data[0].get('bot_token'):
                            sub_token = b_data[0]['bot_token']
                            async with httpx.AsyncClient(timeout=10.0) as http:
                                for u in users:
                                    uid = u.get('telegram_user_id')
                                    if not uid: continue
                                    try:
                                        r = await http.post(
                                            f"https://api.telegram.org/bot{sub_token}/sendMessage",
                                            json={"chat_id": int(uid), "text": raw_text}
                                        )
                                        if r.status_code == 200 and r.json().get("ok"):
                                            users_sent += 1
                                        await asyncio.sleep(0.05)
                                    except Exception:
                                        pass
            except Exception as ex:
                logger.error(f"Error sending to audience: {ex}")

            # 5. Clean up temporary media
            if media_path and os.path.exists(media_path):
                try:
                    os.remove(media_path)
                except Exception:
                    pass

            # 6. Update task in Supabase
            if task_id:
                final_status = 'completed' if (channel_sent or users_sent > 0) else 'failed'
                await supabase.table('tg_broadcast_tasks').update({'status': final_status}).eq('id', task_id).execute()

            # 7. Provide complete, helpful response to the user
            if channel_sent:
                success_msg = f"✅ **Broadcast successfully sent!**\n\n📢 **Channel:** {channel_name}"
                if users_total > 0:
                    success_msg += f"\n👥 **Audience Delivered:** {users_sent}/{users_total} members"
                await event.edit(success_msg)
            elif users_sent > 0:
                await event.edit(
                    f"✅ **Broadcast delivered to audience!**\n\n"
                    f"👥 **Audience Delivered:** {users_sent}/{users_total} members\n\n"
                    f"⚠️ **Channel Note:** Could not post directly to **{channel_name}** because @Gapgrowbot is not an admin in the channel with 'Post Messages' permission."
                )
            else:
                await event.edit(
                    f"❌ **Broadcast delivery failed for {channel_name}.**\n\n"
                    f"👉 **How to fix:**\n"
                    f"1. Open channel **{channel_name}** in Telegram\n"
                    f"2. Go to **Channel Settings > Administrators > Add Administrator**\n"
                    f"3. Search for **@Gapgrowbot**\n"
                    f"4. Give it **Post Messages** permission and Save!\n\n"
                    f"Then use /send again to broadcast."
                )

            user_states.pop(sender_id, None)

        except Exception as e:
            logger.error(f"Confirm error: {e}", exc_info=True)
            await event.edit(f"❌ Failed to process broadcast: {str(e)}")
            user_states.pop(sender_id, None)

    @client.on(events.CallbackQuery(data='cancel_broadcast'))
    async def cancel_handler(event):
        user_states.pop(event.sender_id, None)
        await event.edit("❌ Broadcast cancelled.")

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
