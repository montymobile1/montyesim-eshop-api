"""Loads supabase_ddl.sql into a disposable PostgreSQL database that looks enough like Supabase for it.

    python tests/rls/supabase_stub.py postgresql://postgres:postgres@localhost:5432/postgres [path/to/ddl.sql]

Creates what Supabase provides before the DDL runs -- the anon / authenticated / service_role roles, the
auth schema (users, sessions, refresh_tokens, uid(), jwt()), the extensions schema and Supabase's default
privileges -- then runs the DDL in one go, stopping at the first error. Used by the CI workflow
(.github/workflows/ddl-checks.yml) for the live checks in tests/rls/test_rls_ddl_assertion.py
(DDL_TEST_DATABASE_URL). Never point it at a real environment: it creates roles and schemas.

Kept in Python, not in a .sql file: supabase_ddl.sql is the only SQL file of the repo (team rule,
tests/maintenance/test_single_ddl.py).
"""
import sys
from pathlib import Path

import psycopg

SKELETON = """
DO $$ BEGIN
    IF to_regrole('anon') IS NULL THEN CREATE ROLE anon NOLOGIN NOINHERIT; END IF;
    IF to_regrole('authenticated') IS NULL THEN CREATE ROLE authenticated NOLOGIN NOINHERIT; END IF;
    IF to_regrole('service_role') IS NULL THEN CREATE ROLE service_role NOLOGIN NOINHERIT BYPASSRLS; END IF;
    IF to_regrole('authenticator') IS NULL THEN CREATE ROLE authenticator LOGIN NOINHERIT; END IF;
    IF to_regrole('supabase_auth_admin') IS NULL THEN CREATE ROLE supabase_auth_admin LOGIN NOINHERIT; END IF;
END $$;
GRANT anon, authenticated, service_role TO authenticator;

CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;

CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (
    instance_id uuid, id uuid PRIMARY KEY, aud varchar(255), role varchar(255), email varchar(255),
    encrypted_password varchar(255), email_confirmed_at timestamptz, invited_at timestamptz,
    confirmation_token varchar(255), confirmation_sent_at timestamptz, recovery_token varchar(255),
    recovery_sent_at timestamptz, last_sign_in_at timestamptz, raw_app_meta_data jsonb, raw_user_meta_data jsonb,
    is_super_admin boolean, created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(), phone text,
    phone_confirmed_at timestamptz, banned_until timestamptz, deleted_at timestamptz,
    is_anonymous boolean NOT NULL DEFAULT false, is_sso_user boolean NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS auth.sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES auth.users ON DELETE CASCADE,
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(), factor_id uuid, aal text,
    not_after timestamptz, refreshed_at timestamp, user_agent text, ip inet, tag text);
CREATE TABLE IF NOT EXISTS auth.refresh_tokens (
    instance_id uuid, id bigserial PRIMARY KEY, token varchar(255), user_id varchar(255), revoked boolean,
    created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(), parent varchar(255),
    session_id uuid REFERENCES auth.sessions ON DELETE CASCADE);
CREATE OR REPLACE FUNCTION auth.jwt() RETURNS jsonb LANGUAGE sql STABLE AS
    $f$ SELECT coalesce(nullif(current_setting('request.jwt.claims', true), ''), '{}')::jsonb $f$;
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS
    $f$ SELECT nullif(auth.jwt() ->> 'sub', '')::uuid $f$;
CREATE OR REPLACE FUNCTION auth.role() RETURNS text LANGUAGE sql STABLE AS $f$ SELECT auth.jwt() ->> 'role' $f$;
CREATE OR REPLACE FUNCTION auth.email() RETURNS text LANGUAGE sql STABLE AS $f$ SELECT auth.jwt() ->> 'email' $f$;

CREATE SCHEMA IF NOT EXISTS storage;
CREATE TABLE IF NOT EXISTS storage.buckets (
    id text PRIMARY KEY, name text, public boolean, owner uuid, created_at timestamptz, updated_at timestamptz,
    file_size_limit bigint, allowed_mime_types text[]);
CREATE TABLE IF NOT EXISTS storage.objects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), bucket_id text, name text, owner uuid, metadata jsonb,
    created_at timestamptz, updated_at timestamptz);
ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;

GRANT USAGE ON SCHEMA auth, extensions, public TO anon, authenticated, service_role;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA auth TO anon, authenticated;
-- Supabase's default privileges: everything created in public is granted to the three API roles
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon, authenticated, service_role;
"""


def load(url: str, ddl: Path) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        database = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute(SKELETON)
        conn.execute(f'ALTER DATABASE "{database}" SET search_path = "$user", public, extensions')
    # a new session, so that the DDL runs with Supabase's search_path, as in the SQL editor
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(ddl.read_text())


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    load(sys.argv[1], Path(sys.argv[2]) if len(sys.argv) > 2 else root / "supabase_ddl.sql")
    print("supabase_ddl.sql loaded")
