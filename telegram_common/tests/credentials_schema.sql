-- Run as database owner in disposable Supabase after the foundation migration.
-- All fixtures roll back. No production credentials or auth user rows required.
BEGIN;

DO $$
DECLARE
    fixture_id uuid := gen_random_uuid();
    bot_id uuid := gen_random_uuid();
    initial_version bigint;
    browser_role text;
    privilege_name text;
    protected boolean;
BEGIN
    SELECT relrowsecurity AND relforcerowsecurity INTO protected
      FROM pg_class WHERE oid = 'public.tg_session_credentials'::regclass;
    IF protected IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'RLS must be enabled and forced';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_policy
               WHERE polrelid = 'public.tg_session_credentials'::regclass) THEN
        RAISE EXCEPTION 'Credential table must not have browser policies';
    END IF;
    FOREACH browser_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        FOREACH privilege_name IN ARRAY ARRAY[
            'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'
        ] LOOP
            IF has_table_privilege(browser_role, 'public.tg_session_credentials', privilege_name) THEN
                RAISE EXCEPTION 'Unexpected browser table privilege';
            END IF;
        END LOOP;
        IF has_function_privilege(browser_role,
            'public.tg_session_credentials_before_update()', 'EXECUTE') THEN
            RAISE EXCEPTION 'Unexpected browser function privilege';
        END IF;
    END LOOP;
    FOREACH privilege_name IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE'] LOOP
        IF NOT has_table_privilege('service_role', 'public.tg_session_credentials', privilege_name) THEN
            RAISE EXCEPTION 'Missing backend table privilege';
        END IF;
    END LOOP;

    INSERT INTO public.tg_session_credentials
        (id, principal_kind, purpose, telegram_owner_id, encrypted_session, key_version)
    VALUES (fixture_id, 'user', 'foundation_test', 9223372036854775806,
            'tg1.v1.dummy-format-only', 'v1');

    -- NULL dashboard IDs must not defeat Telegram-user uniqueness.
    BEGIN
        INSERT INTO public.tg_session_credentials
            (principal_kind, purpose, telegram_owner_id, encrypted_session, key_version)
        VALUES ('user', 'foundation_test', 9223372036854775806,
                'tg1.v1.dummy-format-only', 'v1');
        RAISE EXCEPTION 'Duplicate Telegram owner/purpose unexpectedly accepted';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;
    -- Same owner is allowed to use a separate credential for another purpose.
    INSERT INTO public.tg_session_credentials
        (principal_kind, purpose, telegram_owner_id, encrypted_session, key_version)
    VALUES ('user', 'foundation_other_test', 9223372036854775806,
            'tg1.v1.dummy-format-only', 'v1');

    INSERT INTO public.tg_session_credentials
        (id, principal_kind, purpose, bot_configuration_id, encrypted_session, key_version)
    VALUES (bot_id, 'bot', 'foundation_test', 'local-test-' || bot_id::text,
            'tg1.v1.dummy-format-only', 'v1');
    BEGIN
        INSERT INTO public.tg_session_credentials
            (principal_kind, purpose, bot_configuration_id, encrypted_session, key_version)
        VALUES ('bot', 'foundation_test', 'local-test-' || bot_id::text,
                'tg1.v1.dummy-format-only', 'v1');
        RAISE EXCEPTION 'Duplicate bot/purpose unexpectedly accepted';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO public.tg_session_credentials
            (principal_kind, purpose, encrypted_session, key_version)
        VALUES ('user', 'foundation_test', 'tg1.v1.dummy-format-only', 'v1');
        RAISE EXCEPTION 'Ownerless user unexpectedly accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    BEGIN
        INSERT INTO public.tg_session_credentials
            (principal_kind, purpose, telegram_owner_id, bot_configuration_id,
             encrypted_session, key_version)
        VALUES ('bot', 'foundation_test', 1, 'invalid-test-bot',
                'tg1.v1.dummy-format-only', 'v1');
        RAISE EXCEPTION 'Bot with Telegram user owner unexpectedly accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    BEGIN
        UPDATE public.tg_session_credentials SET encrypted_session = 'plaintext'
        WHERE id = fixture_id;
        RAISE EXCEPTION 'Plaintext session unexpectedly accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    BEGIN
        UPDATE public.tg_session_credentials SET key_version = 'v2'
        WHERE id = fixture_id;
        RAISE EXCEPTION 'Mismatched key version unexpectedly accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    BEGIN
        UPDATE public.tg_session_credentials SET purpose = 'changed_identity'
        WHERE id = fixture_id;
        RAISE EXCEPTION 'Identity change unexpectedly accepted' USING ERRCODE = '23514';
    EXCEPTION WHEN raise_exception THEN NULL;
    END;

    SELECT version INTO initial_version FROM public.tg_session_credentials WHERE id = fixture_id;
    UPDATE public.tg_session_credentials SET state = 'revoking', version = 999
    WHERE id = fixture_id AND version = initial_version;
    IF (SELECT version FROM public.tg_session_credentials WHERE id = fixture_id) <> initial_version + 1 THEN
        RAISE EXCEPTION 'Version must increment exactly once';
    END IF;
    UPDATE public.tg_session_credentials SET state = 'active'
    WHERE id = fixture_id AND version = initial_version;
    IF FOUND THEN
        RAISE EXCEPTION 'Stale compare-and-swap unexpectedly succeeded';
    END IF;
    BEGIN
        UPDATE public.tg_session_credentials SET state = 'revoked', revoked_at = now()
        WHERE id = fixture_id;
        RAISE EXCEPTION 'Revoked row retained payload';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    UPDATE public.tg_session_credentials
    SET state = 'revoked', revoked_at = now(), encrypted_session = NULL, key_version = NULL
    WHERE id = fixture_id;
    BEGIN
        INSERT INTO public.tg_session_credentials
            (principal_kind, purpose, telegram_owner_id, encrypted_session, key_version)
        VALUES ('user', 'foundation_test', 9223372036854775806,
                'tg1.v1.dummy-format-only', 'v1');
        RAISE EXCEPTION 'Revoked identity uniqueness bypassed';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;
END;
$$;

ROLLBACK;
