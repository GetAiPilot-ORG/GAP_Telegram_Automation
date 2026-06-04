import os
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from supabase import create_client, Client
from telethon import TelegramClient, events, functions, types
import openai
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import urllib.request
import urllib.parse
import json

# ---- 1. Logging Setup ----
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("LLMBotManager")

# Load environment variables
load_dotenv()

SUPABASE_URL = os.environ.get("VITE_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("VITE_SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL and Key must be defined in environment variables")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Dictionary to store running clients and their tasks
active_clients = {}
running_tasks = {}

# Dictionary to cache the bot configurations from the database
GLOBAL_BOT_CONFIGS = {} 

# Global set of bot IDs that are currently active join bots (have channel mappings)
GLOBAL_JOIN_BOT_IDS = set()

def configure_bot_allowed_updates(token: str):
    """Ensure Bot API webhook is deleted and allowed_updates is set for MTProto (Requirement 4)"""
    try:
        allowed_updates = ["message", "business_connection", "business_message", "edited_business_message", "deleted_business_messages"]
        url = f"https://api.telegram.org/bot{token}/deleteWebhook"
        data = urllib.parse.urlencode({
            "drop_pending_updates": "true",
            "allowed_updates": json.dumps(allowed_updates)
        }).encode("utf-8")
        
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            if res_data.get("ok"):
                logger.info(f"Successfully configured allowed_updates for bot token {token[:10]}...")
            else:
                logger.warning(f"Failed to configure allowed_updates: {res_data.get('description')}")
    except Exception as e:
        logger.error(f"Error configuring bot allowed_updates: {e}")

API_ID = int(os.environ.get("TELEGRAM_API_ID", "12345678"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "dummyhash")

# ---- 2. Supabase Optimization (Thread Pool) ----
supabase_executor = ThreadPoolExecutor(max_workers=20)

async def run_supabase_query(query):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(supabase_executor, query.execute)

# Ensure sessions directory exists
if not os.path.exists("sessions"):
    os.makedirs("sessions")

async def generate_llm_response(bot_id: str, user_message: str, telegram_user_id: int = None) -> str:
    """Generate a response using the configured LLM API key and conversation history from telegram_chat_messages."""
    config = GLOBAL_BOT_CONFIGS.get(bot_id)
    if not config:
        return "Sorry, my configuration is currently unavailable."
    
    if hasattr(config, "get"):
        provider = config.get("provider", "").lower()
        api_key = config.get("api_key")
        business_info = config.get("business_info", "")
        support_name = config.get("support_name", "AI Assistant")
        # knowledge_base_text is the n8n-generated full system prompt
        knowledge_base_text = config.get("knowledge_base_text") or ""
    else:
        provider = getattr(config, "provider", "").lower()
        api_key = getattr(config, "api_key", None)
        business_info = getattr(config, "business_info", "")
        support_name = getattr(config, "support_name", "AI Assistant")
        knowledge_base_text = getattr(config, "knowledge_base_text", "") or ""

    # Priority:
    # 1. knowledge_base_text  → written by n8n after generating the full KB system prompt
    # 2. business_info        → raw user input (used before n8n has run, or as fallback)
    # 3. Generic fallback
    if knowledge_base_text.strip():
        system_prompt = knowledge_base_text
    elif business_info and len(business_info) > 200:
        # Legacy: full prompt was stored directly in business_info
        system_prompt = business_info
    else:
        system_prompt = f"Your name is {support_name}. {business_info or 'You are a helpful AI support assistant.'}"
    
    if not api_key:
        return "⚠️ Setup Error: The API key for this bot has not been configured."

    logger.info(f"generate_llm_response called: bot_id={bot_id}, telegram_user_id={telegram_user_id}, user_message={user_message[:50]}")

    # Fetch conversation history from dedicated telegram_chat_messages table
    history = []
    if telegram_user_id:
        try:
            history_query = supabase.table('telegram_chat_messages')\
                .select('role, content')\
                .eq('bot_id', bot_id)\
                .eq('telegram_user_id', telegram_user_id)\
                .order('created_at', desc=True)\
                .limit(20)
            res = await run_supabase_query(history_query)
            if res.data:
                history = list(reversed(res.data))
                logger.info(f"Fetched {len(history)} messages from dedicated telegram_chat_messages history for user {telegram_user_id}.")
                for idx, msg in enumerate(history):
                    logger.info(f"  History msg [{idx}] - {msg.get('role')}: {msg.get('content')[:60]}...")
            else:
                logger.warning(f"No history messages found for user {telegram_user_id} in telegram_chat_messages.")
        except Exception as e:
            logger.error(f"Failed to fetch chat history for user {telegram_user_id}: {e}")
    else:
        logger.warning(f"generate_llm_response: telegram_user_id is None, skipping history fetch.")
        
    try:
        # ---- OpenAI Handler ----
        if "openai" in provider:
            client = openai.AsyncOpenAI(api_key=api_key)
            
            messages = [{"role": "system", "content": system_prompt}]
            
            # Format and append history
            has_current_message = False
            for msg in history:
                messages.append({"role": msg.get("role"), "content": msg.get("content")})
                if msg.get("role") == "user" and msg.get("content") == user_message:
                    has_current_message = True
            
            # If the current message wasn't in history, append it
            if not has_current_message:
                messages.append({"role": "user", "content": user_message})

            logger.info(f"Sending {len(messages)} messages to OpenAI for bot {bot_id}:")
            for idx, msg in enumerate(messages):
                logger.info(f"  OpenAI msg [{idx}] role={msg.get('role')}: {msg.get('content')[:100]}...")

            response = await client.chat.completions.create(
                model="gpt-3.5-turbo", # Default fast model
                messages=messages,
                max_tokens=600
            )
            return response.choices[0].message.content

        # ---- Gemini Handler ----
        elif "gemini" in provider:
            # Re-configure for each call since genai uses global config in older versions,
            # or pass api_key directly to GenerativeModel if supported.
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-1.5-flash',
                                        system_instruction=system_prompt)
            # Disable safety settings which often block legitimate business queries
            safety_settings = {
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            }
            
            gemini_contents = []
            has_current_message = False
            for msg in history:
                role = "model" if msg.get("role") == "assistant" else "user"
                gemini_contents.append({"role": role, "parts": [msg.get("content")]})
                if msg.get("role") == "user" and msg.get("content") == user_message:
                    has_current_message = True
            
            # If current message wasn't in history, append it
            if not has_current_message:
                gemini_contents.append({"role": "user", "parts": [user_message]})

            logger.info(f"Sending {len(gemini_contents)} turns to Gemini for bot {bot_id}:")
            for idx, turn in enumerate(gemini_contents):
                logger.info(f"  Gemini turn [{idx}] role={turn.get('role')}: {turn.get('parts')[0][:100]}...")

            # Since genai usually runs synchronously, run in executor
            def _generate():
                try:
                    res = model.generate_content(gemini_contents, safety_settings=safety_settings)
                    return res.text
                except Exception as ex:
                    logger.error(f"Gemini error: {ex}")
                    raise
                
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(supabase_executor, _generate)

        else:
            return f"⚠️ Unsupported AI provider: {provider}"
            
    except Exception as e:
        error_msg = str(e).lower()
        if "api key" in error_msg or "unauthorized" in error_msg or "authentication" in error_msg or "invalid_api_key" in error_msg:
            return "⚠️ Setup Error: The provided LLM API key is invalid or has expired. Please update it in the dashboard dashboard."
        elif "quota" in error_msg or "rate limit" in error_msg:
            return "⚠️ Service Error: The LLM provider quota has been exceeded or rate-limited."
        else:
            logger.error(f"LLM Error for bot {bot_id}: {e}")
            return "⚠️ An error occurred while generating a response. Please try again later."

async def get_or_create_telegram_session(bot_id: str, telegram_user_id: int, user_name: str) -> str:
    """Find or create a chatbot session for a Telegram user chatting with a specific bot."""
    try:
        # 1. Try to find existing session mapping
        query_select = supabase.table('telegram_bot_sessions')\
            .select('id')\
            .eq('bot_id', bot_id)\
            .eq('telegram_user_id', telegram_user_id)
        
        res = await run_supabase_query(query_select)
        if res.data and len(res.data) > 0:
            return res.data[0]['id']
            
        # 2. If not found, create new session in chatbot_sessions
        query_insert_session = supabase.table('chatbot_sessions')\
            .insert({'status': 'active'})\
            .select('id')
            
        session_res = await run_supabase_query(query_insert_session)
        if not session_res.data or len(session_res.data) == 0:
            raise ValueError("Failed to create chatbot session")
            
        session_id = session_res.data[0]['id']
        
        # 3. Create mapping in telegram_bot_sessions
        query_insert_mapping = supabase.table('telegram_bot_sessions')\
            .insert({
                'id': session_id,
                'bot_id': bot_id,
                'telegram_user_id': telegram_user_id,
                'user_name': user_name
            })
            
        await run_supabase_query(query_insert_mapping)
        return session_id
        
    except Exception as e:
        logger.error(f"Error in get_or_create_telegram_session: {e}")
        raise

async def start_bot(config: dict):
    bot_id = config['bot_id']
    token = config['bot_token']
    logger.info(f"Starting LLM bot: {bot_id}")
    
    # Configure allowed updates for bot (Requirement 4)
    configure_bot_allowed_updates(token)
    
    try:
        # Load from the same sessions directory as bot.py
        client = TelegramClient(f"sessions/llm_bot_{bot_id}", API_ID, API_HASH)
        await client.start(bot_token=token)
        logger.info(f"LLM Bot {bot_id} started successfully!")
        
        @client.on(events.NewMessage)
        async def handler(event):
            # Only respond to private messages
            if not event.is_private:
                return

            # Exclude service messages or empty messages
            if event.message.action or not event.message.text:
                return

            user_message = event.message.text

            # Ignore /start commands ONLY IF this bot is also configured as a Join Bot (has active channel mappings)
            if user_message.strip().startswith('/start'):
                if bot_id in GLOBAL_JOIN_BOT_IDS:
                    logger.info(f"LLM Bot {bot_id}: Ignoring /start because it has active channel mappings (handled by Join Bot).")
                    return
                # Otherwise, let the LLM handle /start to welcome the user (pure support bot)

            user_id = event.sender_id
            
            logger.info(f"LLM Bot {bot_id}: Received message from {user_id}: {user_message[:50]}...")
            
            # Fetch sender details to construct user name
            user_name = f"User {user_id}"
            try:
                sender = await event.get_sender()
                if sender:
                    first_name = getattr(sender, 'first_name', '') or ''
                    last_name = getattr(sender, 'last_name', '') or ''
                    username = getattr(sender, 'username', '') or ''
                    
                    full_name = f"{first_name} {last_name}".strip()
                    if full_name:
                        user_name = full_name
                    elif username:
                        user_name = username
            except Exception as e:
                logger.error(f"Failed to fetch sender profile: {e}")

            # Get or create the mapped Supabase session ID (legacy)
            session_id = None
            try:
                session_id = await get_or_create_telegram_session(bot_id, user_id, user_name)
            except Exception as e:
                logger.error(f"Failed to resolve session ID for Telegram chat: {e}")

            # 1. Save the user's message to legacy chatbot_messages
            if session_id:
                try:
                    user_msg_query = supabase.table('chatbot_messages').insert({
                        'session_id': session_id,
                        'role': 'user',
                        'content': user_message
                    })
                    await run_supabase_query(user_msg_query)
                except Exception as e:
                    logger.error(f"Failed to save user message to legacy chatbot_messages: {e}")
            
            # 2. Save the user's message to dedicated telegram_chat_messages
            try:
                tg_user_msg_query = supabase.table('telegram_chat_messages').insert({
                    'bot_id': bot_id,
                    'telegram_user_id': user_id,
                    'user_name': user_name,
                    'role': 'user',
                    'content': user_message
                })
                await run_supabase_query(tg_user_msg_query)
            except Exception as e:
                logger.error(f"Failed to save user message to telegram_chat_messages: {e}")
            
            # Show "typing..." status and get response
            async with client.action(event.chat_id, 'typing'):
                response = await generate_llm_response(bot_id, user_message, user_id)
                await event.respond(response)
                
                # 3. Save the bot's response to legacy chatbot_messages
                if session_id:
                    try:
                        bot_msg_query = supabase.table('chatbot_messages').insert({
                            'session_id': session_id,
                            'role': 'assistant',
                            'content': response
                        })
                        await run_supabase_query(bot_msg_query)
                    except Exception as e:
                        logger.error(f"Failed to save bot response to legacy chatbot_messages: {e}")

                # 4. Save the bot's response to dedicated telegram_chat_messages
                try:
                    tg_bot_msg_query = supabase.table('telegram_chat_messages').insert({
                        'bot_id': bot_id,
                        'telegram_user_id': user_id,
                        'user_name': user_name,
                        'role': 'assistant',
                        'content': response
                    })
                    await run_supabase_query(tg_bot_msg_query)
                except Exception as e:
                    logger.error(f"Failed to save bot response to telegram_chat_messages: {e}")


        @client.on(events.Raw)
        async def raw_handler(event):
            # In Telethon events.Raw, the handler is called with the raw update object directly (Requirement 7)
            update = event
            if not update:
                return

            # Log all incoming raw updates for tracking (Requirement 5)
            logger.info(f"LLM Bot {bot_id} (Raw Event): Received incoming update type {type(update).__name__}")

            # Support business_connection update (Requirement 2)
            if isinstance(update, types.UpdateBotBusinessConnection):
                connection = update.connection
                logger.info(
                    f"LLM Bot {bot_id} (business_connection): "
                    f"Connection ID: {connection.connection_id}, "
                    f"User ID: {connection.user_id}, "
                    f"Disabled: {connection.disabled}"
                )
                return

            # Support edited_business_message update (Requirement 2)
            if isinstance(update, types.UpdateBotEditBusinessMessage):
                msg = update.message
                logger.info(
                    f"LLM Bot {bot_id} (edited_business_message): "
                    f"Message ID: {msg.id} edited in connection: {update.connection_id}"
                )
                return

            # Support deleted_business_messages update (Requirement 2)
            if isinstance(update, types.UpdateBotDeleteBusinessMessages):
                logger.info(
                    f"LLM Bot {bot_id} (deleted_business_messages): "
                    f"Messages: {update.messages} deleted in connection: {update.connection_id}"
                )
                return

            # Support business_message update (Requirement 2)
            if not isinstance(update, types.UpdateBotNewBusinessMessage):
                return

            msg = update.message
            if not msg or getattr(msg, 'out', False) or not getattr(msg, 'message', ''):
                return

            # Extract fields (Requirement 3)
            connection_id = update.connection_id
            
            # Extract chat_id
            peer = msg.peer_id
            if hasattr(peer, 'user_id'):
                chat_id = peer.user_id
            elif hasattr(peer, 'channel_id'):
                chat_id = peer.channel_id
            elif hasattr(peer, 'chat_id'):
                chat_id = peer.chat_id
            else:
                chat_id = getattr(msg, 'chat_id', None)
                
            user_message = msg.message

            # Extract user_id/sender_id
            user_id = getattr(msg, 'sender_id', None)
            if not user_id:
                if hasattr(msg, 'from_id') and hasattr(msg.from_id, 'user_id'):
                    user_id = msg.from_id.user_id
                else:
                    user_id = chat_id or getattr(msg, 'chat_id', None)

            if not user_id:
                logger.error("Could not determine sender/user_id for business message")
                return

            logger.info(
                f"LLM Bot {bot_id} (business_message): Received message. "
                f"business_connection_id={connection_id}, chat_id={chat_id}, text={user_message[:50]}..."
            )

            # Fetch sender details to construct user name
            user_name = f"User {user_id}"
            try:
                sender = await client.get_entity(user_id)
                if sender:
                    first_name = getattr(sender, 'first_name', '') or ''
                    last_name = getattr(sender, 'last_name', '') or ''
                    username = getattr(sender, 'username', '') or ''
                    
                    full_name = f"{first_name} {last_name}".strip()
                    if full_name:
                        user_name = full_name
                    elif username:
                        user_name = username
            except Exception as e:
                logger.error(f"Failed to fetch sender profile: {e}")

            # Get or create the mapped Supabase session ID (legacy)
            session_id = None
            try:
                session_id = await get_or_create_telegram_session(bot_id, user_id, user_name)
            except Exception as e:
                logger.error(f"Failed to resolve session ID for Telegram chat: {e}")

            # 1. Save the user's message to legacy chatbot_messages
            if session_id:
                try:
                    user_msg_query = supabase.table('chatbot_messages').insert({
                        'session_id': session_id,
                        'role': 'user',
                        'content': user_message
                    })
                    await run_supabase_query(user_msg_query)
                except Exception as e:
                    logger.error(f"Failed to save user message to legacy chatbot_messages: {e}")
            
            # 2. Save the user's message to dedicated telegram_chat_messages
            try:
                tg_user_msg_query = supabase.table('telegram_chat_messages').insert({
                    'bot_id': bot_id,
                    'telegram_user_id': user_id,
                    'user_name': user_name,
                    'role': 'user',
                    'content': user_message
                })
                await run_supabase_query(tg_user_msg_query)
            except Exception as e:
                logger.error(f"Failed to save user message to telegram_chat_messages: {e}")
            
            # Generate response
            response = await generate_llm_response(bot_id, user_message, user_id)
            
            # Send message via business connection
            try:
                try:
                    peer = await client.get_input_entity(msg.peer_id)
                except Exception:
                    peer = msg.peer_id

                send_msg_req = functions.messages.SendMessageRequest(
                    peer=peer,
                    message=response,
                    reply_to=types.InputMessageReplyToMessage(reply_to_msg_id=msg.id)
                )
                
                await client(functions.InvokeWithBusinessConnectionRequest(
                    connection_id=connection_id,
                    query=send_msg_req
                ))
            except Exception as e:
                logger.error(f"Failed to send response via business connection: {e}")
                return

            # 3. Save the bot's response to legacy chatbot_messages
            if session_id:
                try:
                    bot_msg_query = supabase.table('chatbot_messages').insert({
                        'session_id': session_id,
                        'role': 'assistant',
                        'content': response
                    })
                    await run_supabase_query(bot_msg_query)
                except Exception as e:
                    logger.error(f"Failed to save bot response to legacy chatbot_messages: {e}")

            # 4. Save the bot's response to dedicated telegram_chat_messages
            try:
                tg_bot_msg_query = supabase.table('telegram_chat_messages').insert({
                    'bot_id': bot_id,
                    'telegram_user_id': user_id,
                    'user_name': user_name,
                    'role': 'assistant',
                    'content': response
                })
                await run_supabase_query(tg_bot_msg_query)
            except Exception as e:
                logger.error(f"Failed to save bot response to telegram_chat_messages: {e}")

        active_clients[bot_id] = client
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Failed to start LLM bot {bot_id}: {e}")


async def bot_runner():
    logger.info("LLM Bot Manager Started. Polling Supabase every 15 seconds for active chatbot configs...")
    while True:
        try:
            # 1. Fetch active channel mappings to identify join bots
            try:
                mappings_query = supabase.table('bot_channel_mappings').select('bot_id').eq('status', 'Active')
                mappings_res = await run_supabase_query(mappings_query)
                GLOBAL_JOIN_BOT_IDS.clear()
                if mappings_res.data:
                    for m in mappings_res.data:
                        if m.get('bot_id'):
                            GLOBAL_JOIN_BOT_IDS.add(m['bot_id'])
                logger.info(f"Active Join Bot IDs in database: {list(GLOBAL_JOIN_BOT_IDS)}")
            except Exception as map_err:
                logger.error(f"Error fetching channel mappings for LLM Bot Manager: {map_err}")

            # 2. Join chatbot_configs with telegram_tracker to get the bot_token
            # Also fetch knowledge_base_text (n8n-generated full system prompt)
            query = supabase.table('chatbot_configs')\
                .select('*, telegram_tracker(bot_token)')\
                .eq('status', 'active')
            
            response = await run_supabase_query(query)
            configs = response.data
            
            if configs is None:
                configs = []
            elif hasattr(configs, "data"):
                # Handle cases where response.data contains another layer of data
                configs = configs.data

            current_bot_ids = set()
            for config in configs:
                bot_id = config['bot_id']
                tracker_data = config.get('telegram_tracker')
                
                # Skip if we couldn't fetch the token
                if not tracker_data or not tracker_data.get('bot_token'):
                    continue
                    
                bot_token = tracker_data['bot_token']
                
                # Merge into a single flat dict
                full_config = {**config, 'bot_token': bot_token}
                
                # Update global cache
                GLOBAL_BOT_CONFIGS[bot_id] = full_config
                current_bot_ids.add(bot_id)
                
                if bot_id not in running_tasks or running_tasks[bot_id].done():
                    if bot_id in running_tasks and running_tasks[bot_id].done():
                        exc = running_tasks[bot_id].exception()
                        if exc:
                            logger.warning(f"LLM Bot {bot_id} task failed ({exc}), restarting...")
                    task = asyncio.create_task(start_bot(full_config))
                    running_tasks[bot_id] = task
                    
            # Check for deleted/paused bots
            for bot_id in list(running_tasks.keys()):
                if bot_id not in current_bot_ids:
                    logger.info(f"LLM Bot {bot_id} is no longer active. Stopping...")
                    running_tasks[bot_id].cancel()
                    
                    if bot_id in GLOBAL_BOT_CONFIGS:
                        GLOBAL_BOT_CONFIGS.pop(bot_id, None)
                        
                    if bot_id in active_clients:
                        try:
                            fut = active_clients[bot_id].disconnect()
                            if asyncio.iscoroutine(fut) or asyncio.isfuture(fut):
                                await fut
                        except Exception as e:
                            logger.error(f"Failed to disconnect bot {bot_id}: {e}")
                        active_clients.pop(bot_id, None)
                        running_tasks.pop(bot_id, None)

        except Exception as e:
            logger.error(f"Error in LLM bot manager loop: {e}")
            
        await asyncio.sleep(15)

if __name__ == "__main__":
    if not os.environ.get("TELEGRAM_API_ID"):
        logger.warning("TELEGRAM_API_ID and TELEGRAM_API_HASH are not set in .env.")
    try:
        asyncio.run(bot_runner())
    except KeyboardInterrupt:
        logger.info("LLM Bot Manager manually stopped.")
