import asyncio
import os
import aiohttp
import random
import string
import datetime
from dotenv import load_dotenv
from supabase import create_async_client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

SUPABASE_URL = os.environ.get('VITE_SUPABASE_URL') or os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY') or os.environ.get('VITE_SUPABASE_ANON_KEY') or os.environ.get('SUPABASE_KEY')

async def sync_all_active_mappings():
    client = await create_async_client(SUPABASE_URL, SUPABASE_KEY)
    maps_res = await client.table('tg_bot_channel_mappings').select('*, bot:tg_tracker(*)').eq('status', 'Active').execute()
    maps = maps_res.data or []
    print(f"Syncing {len(maps)} active mappings...")
    
    async with aiohttp.ClientSession() as session:
        for m in maps:
            bot = m.get('bot')
            if not bot or not bot.get('bot_token'):
                continue
            bot_token = bot['bot_token']
            bot_id = bot['id']
            user_id = bot.get('user_id')
            mapping_id = m['id']
            cid_str = str(m['channel_id'])
            full_cid = cid_str if cid_str.startswith("-100") else f"-100{cid_str}"
            
            try:
                print(f"Processing mapping {mapping_id} on {full_cid}...")
            except: pass
            
            fetched_invites = []
            seen_links = set()
            
            # 1. getChat
            try:
                async with session.get(f"https://api.telegram.org/bot{bot_token}/getChat?chat_id={full_cid}") as resp:
                    chat_data = await resp.json()
                    if chat_data.get('ok') and chat_data.get('result', {}).get('invite_link'):
                        p_link = chat_data['result']['invite_link']
                        seen_links.add(p_link)
                        fetched_invites.append({
                            'link': p_link,
                            'title': "Primary Channel Link",
                            'request_needed': False
                        })
            except Exception as e_gc:
                print("getChat note:", e_gc)
                
            # 2. Keep known tracked links for this channel
            db_res = await client.table('tg_bot_join_links').select('*').eq('channel_mapping_id', mapping_id).execute()
            for existing_item in (getattr(db_res, 'data', []) or []):
                ex_link = existing_item.get('invite_link')
                if ex_link and ex_link not in seen_links:
                    seen_links.add(ex_link)
                    fetched_invites.append({
                        'link': ex_link,
                        'title': existing_item.get('name') or "Channel Invite Link",
                        'request_needed': bool(existing_item.get('is_request_needed', False))
                    })
                    
            for inv in fetched_invites:
                link_url = inv['link']
                chk = await client.table('tg_bot_join_links').select('id').eq('bot_id', bot_id).eq('invite_link', link_url).execute()
                if not chk.data:
                    slug = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
                    payload = {
                        "user_id": user_id,
                        "bot_id": bot_id,
                        "channel_mapping_id": mapping_id,
                        "name": inv['title'],
                        "slug": slug,
                        "invite_link": link_url,
                        "is_auto_fetched": True,
                        "is_request_needed": inv.get('request_needed', False),
                        "telegram_message": "Click the button below to join the private channel.",
                        "button_text": "Join Channel"
                    }
                    await client.table('tg_bot_join_links').insert(payload).execute()
                    print(f"  + Added: {inv['title']}")
                else:
                    print(f"  - Exists: {inv['title']}")
                    
            if fetched_invites:
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                await client.table('tg_bot_channel_mappings').update({
                    'invite_link': fetched_invites[0]['link'],
                    'last_links_synced_at': now_iso,
                    'sync_links_requested': False
                }).eq('id', mapping_id).execute()

    print("All active mappings synced successfully!")

if __name__ == '__main__':
    asyncio.run(sync_all_active_mappings())
