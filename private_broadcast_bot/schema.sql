-- Schema for Private Broadcast Bot
-- Supabase / PostgreSQL Multi-Tenant Migration & Upgrade Script

-- 1. Multi-Tenant Bots Configuration Table
CREATE TABLE IF NOT EXISTS telegram_private_broadcast_bots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    bot_token TEXT NOT NULL,
    bot_name TEXT,
    bot_username TEXT,
    relay_chat_id TEXT NOT NULL,
    welcome_message TEXT DEFAULT '👋 Welcome to our Private Announcement Bot!\n\nYou will receive real-time updates directly in this chat.\n\nUse /stop to unsubscribe at any time.',
    is_active BOOLEAN DEFAULT TRUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    CONSTRAINT unique_user_bot UNIQUE (user_id, bot_token)
);

CREATE INDEX IF NOT EXISTS idx_tg_pb_bots_user ON telegram_private_broadcast_bots(user_id);
CREATE INDEX IF NOT EXISTS idx_tg_pb_bots_active ON telegram_private_broadcast_bots(is_active);

-- 2. Table for subscribers (private chat users who started a bot)
CREATE TABLE IF NOT EXISTS telegram_private_broadcast_users (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    is_active BOOLEAN DEFAULT TRUE NOT NULL,
    started_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

-- Ensure multi-tenant & owner isolation columns exist on telegram_private_broadcast_users
ALTER TABLE telegram_private_broadcast_users 
    ADD COLUMN IF NOT EXISTS bot_id UUID REFERENCES telegram_private_broadcast_bots(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS owner_tg_user_id BIGINT,
    ADD COLUMN IF NOT EXISTS source_chat_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_tg_pb_users_active ON telegram_private_broadcast_users(is_active);
CREATE INDEX IF NOT EXISTS idx_tg_pb_users_tg_id ON telegram_private_broadcast_users(telegram_user_id);
CREATE INDEX IF NOT EXISTS idx_tg_pb_users_bot ON telegram_private_broadcast_users(bot_id);
CREATE INDEX IF NOT EXISTS idx_tg_pb_users_owner ON telegram_private_broadcast_users(owner_tg_user_id);

-- 3. Table for broadcast jobs
CREATE TABLE IF NOT EXISTS telegram_private_broadcast_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_chat_id BIGINT NOT NULL,
    source_message_id BIGINT NOT NULL,
    grouped_id BIGINT NULL,
    message_type TEXT DEFAULT 'single' NOT NULL,
    source_message_ids JSONB NULL,
    status TEXT DEFAULT 'pending' NOT NULL,
    total_recipients INT DEFAULT 0 NOT NULL,
    sent_count INT DEFAULT 0 NOT NULL,
    failed_count INT DEFAULT 0 NOT NULL,
    blocked_count INT DEFAULT 0 NOT NULL,
    error_message TEXT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    started_at TIMESTAMPTZ NULL,
    completed_at TIMESTAMPTZ NULL
);

-- Ensure multi-tenant & owner isolation columns exist on telegram_private_broadcast_jobs
ALTER TABLE telegram_private_broadcast_jobs 
    ADD COLUMN IF NOT EXISTS bot_id UUID REFERENCES telegram_private_broadcast_bots(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS owner_tg_user_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_tg_pb_jobs_status ON telegram_private_broadcast_jobs(status);
CREATE INDEX IF NOT EXISTS idx_tg_pb_jobs_bot ON telegram_private_broadcast_jobs(bot_id);
CREATE INDEX IF NOT EXISTS idx_tg_pb_jobs_owner ON telegram_private_broadcast_jobs(owner_tg_user_id);

-- 4. Table for individual delivery tracking per recipient per job
CREATE TABLE IF NOT EXISTS telegram_private_broadcast_deliveries (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES telegram_private_broadcast_jobs(id) ON DELETE CASCADE,
    telegram_user_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    status TEXT DEFAULT 'pending' NOT NULL,
    error_code TEXT NULL,
    error_message TEXT NULL,
    sent_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    CONSTRAINT unique_job_user UNIQUE (job_id, telegram_user_id)
);

-- Ensure multi-tenant column exists on telegram_private_broadcast_deliveries
ALTER TABLE telegram_private_broadcast_deliveries 
    ADD COLUMN IF NOT EXISTS bot_id UUID REFERENCES telegram_private_broadcast_bots(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_tg_pb_deliv_job_status ON telegram_private_broadcast_deliveries(job_id, status);

-- Enable Row Level Security (RLS) for Web Dashboard
ALTER TABLE telegram_private_broadcast_bots ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_private_broadcast_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_private_broadcast_jobs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can manage their own broadcast bots" ON telegram_private_broadcast_bots;
CREATE POLICY "Users can manage their own broadcast bots" 
    ON telegram_private_broadcast_bots FOR ALL 
    USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can view subscribers of their bots" ON telegram_private_broadcast_users;
CREATE POLICY "Users can view subscribers of their bots" 
    ON telegram_private_broadcast_users FOR ALL 
    USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can view broadcast jobs of their bots" ON telegram_private_broadcast_jobs;
CREATE POLICY "Users can view broadcast jobs of their bots" 
    ON telegram_private_broadcast_jobs FOR ALL 
    USING (auth.uid() = user_id);
 