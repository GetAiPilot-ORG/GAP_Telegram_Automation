import os
import logging
from typing import List, Set
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)

# Load local .env first, then fallback to root .env
LOCAL_ENV = os.path.join(BASE_DIR, ".env")
ROOT_ENV = os.path.join(ROOT_DIR, ".env")

if os.path.exists(LOCAL_ENV):
    load_dotenv(LOCAL_ENV)
if os.path.exists(ROOT_ENV):
    load_dotenv(ROOT_ENV)
load_dotenv()  # Fallback to current working directory

# Environment Variables
API_ID = int(os.getenv("TELEGRAM_API_ID", os.getenv("API_ID", "0")))
API_HASH = os.getenv("TELEGRAM_API_HASH", os.getenv("API_HASH", "")).strip()
BOT_TOKEN = os.getenv("PRIVATE_BROADCAST_BOT_TOKEN", "").strip()

# Supabase configuration
SUPABASE_URL = (os.getenv("SUPABASE_URL") or os.getenv("VITE_SUPABASE_URL", "")).strip()
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY") 
    or os.getenv("SUPABASE_KEY") 
    or os.getenv("VITE_SUPABASE_ANON_KEY", "")
).strip()

# Relay & Admin settings
RELAY_CHAT_ID_RAW = os.getenv("PRIVATE_BROADCAST_RELAY_CHAT_ID", "").strip()
ADMIN_IDS_RAW = os.getenv("PRIVATE_BROADCAST_ADMIN_IDS", "").strip()

def parse_chat_ids(raw: str) -> Set[int]:
    result = set()
    if not raw:
        return result
    for item in raw.split(","):
        item = item.strip()
        if item:
            try:
                result.add(int(item))
            except ValueError:
                pass
    return result

RELAY_CHAT_IDS: Set[int] = parse_chat_ids(RELAY_CHAT_ID_RAW)
ADMIN_IDS: Set[int] = parse_chat_ids(ADMIN_IDS_RAW)

# Performance & Rate limiting config
BATCH_SIZE = int(os.getenv("BROADCAST_BATCH_SIZE", "50"))
DELAY_BETWEEN_MESSAGES = float(os.getenv("BROADCAST_DELAY_BETWEEN_MESSAGES", "0.05"))

WELCOME_MESSAGE = os.getenv(
    "WELCOME_MESSAGE",
    "👋 **Welcome!** You have subscribed to private updates.\n\nYou will receive important announcements directly here.\nType **/stop** anytime to unsubscribe."
)

# Logging configuration
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    format="[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "private_broadcast_bot.log"), encoding="utf-8")
    ]
)

logger = logging.getLogger("PRIVATE-BROADCAST")

# Quiet down noisy third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

def validate_config():
    missing = []
    if not BOT_TOKEN or "ABCdefGHIjklMNOpqrsTUVwxyZ" in BOT_TOKEN:
        missing.append("PRIVATE_BROADCAST_BOT_TOKEN (please set a real token from @BotFather in .env)")
    if not SUPABASE_URL or "your-supabase-project" in SUPABASE_URL:
        missing.append("SUPABASE_URL")
    if not SUPABASE_KEY or "your_supabase" in SUPABASE_KEY:
        missing.append("SUPABASE_KEY / SUPABASE_SERVICE_ROLE_KEY")
    if not RELAY_CHAT_IDS:
        logger.warning("[PRIVATE-BROADCAST] Warning: PRIVATE_BROADCAST_RELAY_CHAT_ID is empty or unparsed in .env!")
    
    if missing:
        raise ValueError(f"Invalid or missing environment variables: {', '.join(missing)}")
