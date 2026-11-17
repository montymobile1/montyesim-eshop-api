"""Read-only pre-go-live security check of a Supabase instance against the SOC hardening baseline (TEOS-329).

Source: SOC "Supabase Estate Security Assessment and Hardening", 20 Oct 2026, section 7 (hardening baseline)
and Appendix A.1 (exposure). A database loaded from supabase_ddl.sql (its last section is the TEOS-329 sweep)
passes every check. The pre-go-live checklist that uses this script: docs/pre-go-live-checklist.md.

Usage:
    SECURITY_CHECK_DATABASE_URL=postgresql://... \\
    SUPABASE_URL=https://<ref>.supabase.co SUPABASE_ANON_KEY=... \\
    venv/bin/python scripts/security_check.py [--log-drain NAME] [--accept ID[:OBJECT]=REASON ...]

Inputs (read from the environment, never from the command line, and never printed):
    SECURITY_CHECK_DATABASE_URL  postgres URL (the session pooler URL of the project works); --database-url-env
                                 names another variable
    SUPABASE_URL                 project URL, for the Auth settings (T8). Without it T8 is UNKNOWN
    SUPABASE_ANON_KEY            any project key is enough for GET /auth/v1/settings; SUPABASE_KEY is used when
                                 SUPABASE_ANON_KEY is not set
Options:
    --log-drain NAME             the log drain configured for the project (dashboard: Project Settings -> Log
                                 Drains), as the operator saw it. Recorded as attested; without it T11b is UNKNOWN
    --min-pg-major N             lowest accepted PostgreSQL major version (T12), default 17
    --schema NAME                schema exposed through the Data API, repeatable, default public
    --accept ID[:OBJECT]=REASON  record a risk the SOC accepted: the check (or only that object of it) is reported
                                 ACCEPTED with the reason instead of FAIL. Repeatable

What it does: SELECTs on the catalogue in a READ ONLY transaction, and one GET of /auth/v1/settings. It writes
nothing and prints key names and object names (tables, functions, policies, buckets, extensions), never a value.

Statuses: PASS, FAIL, WARN (reported, not blocking), ACCEPTED (a FAIL the SOC accepted, with its reason),
UNKNOWN (the input for the check was not given or not readable).
Exit codes: 0 every check PASS / WARN / ACCEPTED; 1 at least one FAIL; 2 could not run (no database URL, cannot
connect, not a Supabase database, bad option); 3 no FAIL, but at least one UNKNOWN (a go-live needs 0).

Check IDs: B1-B6 are the baseline checks of supabase_ddl.sql (RLS, policies, grants, default privileges). T4, T5,
T6, T8, T10, T11, T12 are the SOC test numbers; a letter splits one SOC test into the parts it reports.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

PASS, FAIL, WARN, ACCEPTED, UNKNOWN = "PASS", "FAIL", "WARN", "ACCEPTED", "UNKNOWN"
EXIT_OK, EXIT_FAIL, EXIT_ERROR, EXIT_UNKNOWN = 0, 1, 2, 3

DEFAULT_MIN_PG_MAJOR = 17
#: The per-user tables: authenticated keeps SELECT on these (row-filtered by the TEOS-268 policies)
PER_USER = ("user_profile", "user_profile_bundle", "user_order", "user_wallet", "user_wallet_transaction",
            "promotion_usage", "device", "notification", "user_otp", "users_copy", "contact_us")

SIGNUP_NOTE = ("needed today: email login of a new customer calls sign_in_with_otp without should_create_user=False "
               "(app/config/config.py authenticate), so GoTrue creates the account and refuses that when sign-ups "
               "are disabled; phone login and the store-review account create users with auth.sign_up "
               "(app/services/auth_service.py). Disabling sign-ups breaks new-customer login until those move to "
               "the admin API")
AUTOCONFIRM_NOTE = ("not needed by email login (the OTP verify confirms the address); phone login signs up a "
                    "placeholder address (<msisdn>_user@esim.com) that nobody can confirm, so without auto-confirm "
                    "GoTrue mails a confirmation to it on every new phone user (bounces, counts against the Auth "
                    "email rate limit). Not tested with auto-confirm off")


@dataclass
class Result:
    id: str
    title: str
    status: str
    objects: list = field(default_factory=list)
    note: str = ""


@dataclass
class Context:
    query: Callable[..., list]           # (sql[, params]) -> rows, in one READ ONLY transaction
    auth_settings: Optional[dict]        # GET /auth/v1/settings, None when not read
    auth_error: str                      # why auth_settings is None
    log_drain: Optional[str]
    min_pg_major: int
    schemas: tuple


# ---------------------------------------------------------------------------------------------------------------
# catalogue helpers
# ---------------------------------------------------------------------------------------------------------------

def _col(ctx: Context, sql: str) -> list:
    """First column of every row. No parameters: the SQL contains format('%s') calls. The API schemas are in the
    transaction-local setting teos329.api_schemas (set by run())."""
    return [row[0] for row in ctx.query(sql)]


def _fail_if(objects: Iterable, id_: str, title: str, status_if_any: str = FAIL, note: str = "") -> Result:
    objects = sorted(objects)
    return Result(id_, title, status_if_any if objects else PASS, objects, note if objects else "")


# ---------------------------------------------------------------------------------------------------------------
# B: baseline of supabase_ddl.sql
# ---------------------------------------------------------------------------------------------------------------

def check_rls(ctx):
    return [_fail_if(_col(ctx, """
        SELECT format('%s.%s (%s)', n.nspname, c.relname,
                      CASE WHEN NOT c.relrowsecurity THEN 'RLS off' ELSE 'not forced' END)
          FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = ANY (string_to_array(current_setting('teos329.api_schemas'), ',')) AND c.relkind IN ('r', 'p')
           AND NOT (c.relrowsecurity AND c.relforcerowsecurity)"""),
        "B1", "RLS enabled and forced on every table")]


def check_open_policies(ctx):
    return [_fail_if(_col(ctx, """
        SELECT format('%s.%s.%s (%s, to %s)', schemaname, tablename, policyname, cmd, array_to_string(roles, ','))
          FROM pg_catalog.pg_policies
         WHERE schemaname = ANY (string_to_array(current_setting('teos329.api_schemas'), ','))
           AND (roles && ARRAY['anon', 'public']::name[]
                OR (roles && ARRAY['authenticated']::name[]
                    AND (lower(btrim(coalesce(qual, ''), '() ')) = 'true'
                         OR lower(btrim(coalesce(with_check, ''), '() ')) = 'true')))"""),
        "B2", "no policy for anon/PUBLIC, no USING(true)/WITH CHECK(true) for authenticated")]


def check_anon_grants(ctx):
    return [_fail_if(_col(ctx, """
        SELECT format('%s.%s (%s)', n.nspname, c.relname, string_agg(p.priv, ',' ORDER BY p.priv))
          FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'USAGE']) p(priv)
         WHERE n.nspname = ANY (string_to_array(current_setting('teos329.api_schemas'), ',')) AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
           AND (CASE WHEN c.relkind = 'S' THEN p.priv IN ('SELECT', 'UPDATE', 'USAGE')
                                               AND has_sequence_privilege('anon', c.oid, p.priv)
                     ELSE p.priv <> 'USAGE' AND has_table_privilege('anon', c.oid, p.priv) END
                OR (p.priv = 'SELECT' AND c.relkind <> 'S' AND has_any_column_privilege('anon', c.oid, 'SELECT')))
         GROUP BY n.nspname, c.relname"""),
        "B3", "anon has no privilege on tables, views or sequences")]


def check_authenticated_writes(ctx):
    return [_fail_if(_col(ctx, """
        SELECT format('%s.%s (%s)', n.nspname, c.relname, string_agg(p.priv, ',' ORDER BY p.priv))
          FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) p(priv)
         WHERE n.nspname = ANY (string_to_array(current_setting('teos329.api_schemas'), ',')) AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND has_table_privilege('authenticated', c.oid, p.priv)
         GROUP BY n.nspname, c.relname"""),
        "B4", "authenticated cannot write or TRUNCATE tables (TRUNCATE ignores RLS)")]


def check_anon_execute(ctx):
    return [_fail_if(_col(ctx, """
        SELECT p.oid::regprocedure::text
          FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = ANY (string_to_array(current_setting('teos329.api_schemas'), ',')) AND NOT p.prosecdef
           AND has_function_privilege('anon', p.oid, 'EXECUTE')
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d
                            WHERE d.classid = 'pg_catalog.pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')"""),
        "B5", "anon cannot execute functions (SECURITY INVOKER; DEFINER is T4)")]


def check_default_privileges(ctx):
    postgres = _col(ctx, """
        SELECT DISTINCT format('postgres %s %s -> %s',
                               CASE WHEN d.defaclnamespace = 0 THEN 'global' ELSE d.defaclnamespace::regnamespace::text END,
                               CASE d.defaclobjtype WHEN 'r' THEN 'tables' WHEN 'S' THEN 'sequences'
                                    WHEN 'f' THEN 'functions' ELSE d.defaclobjtype::text END,
                               CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(a.grantee)::text END)
          FROM pg_catalog.pg_default_acl d, aclexplode(d.defaclacl) a
         WHERE d.defaclrole = 'postgres'::regrole AND d.defaclobjtype IN ('r', 'S', 'f')
           AND (d.defaclnamespace = 0 OR d.defaclnamespace::regnamespace::text = ANY (string_to_array(current_setting('teos329.api_schemas'), ',')))
           AND (a.grantee = 0 OR pg_catalog.pg_get_userbyid(a.grantee) IN ('anon', 'authenticated'))
        UNION ALL
        -- no global entry for functions = PostgreSQL's built-in default: PUBLIC executes every new function
        SELECT 'postgres global functions -> PUBLIC (built-in default)'
         WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_default_acl
                            WHERE defaclrole = 'postgres'::regrole AND defaclnamespace = 0 AND defaclobjtype = 'f')""")
    supabase_admin = _col(ctx, """
        SELECT DISTINCT format('supabase_admin %s %s -> %s', d.defaclnamespace::regnamespace,
                               CASE d.defaclobjtype WHEN 'r' THEN 'tables' WHEN 'S' THEN 'sequences'
                                    WHEN 'f' THEN 'functions' ELSE d.defaclobjtype::text END,
                               CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(a.grantee)::text END)
          FROM pg_catalog.pg_default_acl d, aclexplode(d.defaclacl) a
         WHERE pg_catalog.pg_get_userbyid(d.defaclrole) = 'supabase_admin'
           AND d.defaclnamespace::regnamespace::text = ANY (string_to_array(current_setting('teos329.api_schemas'), ','))
           AND (a.grantee = 0 OR pg_catalog.pg_get_userbyid(a.grantee) IN ('anon', 'authenticated'))""")
    return [_fail_if(postgres, "B6", "default privileges of postgres grant nothing to anon/authenticated/PUBLIC"),
            _fail_if(supabase_admin, "B6s", "default privileges of supabase_admin (only postgres-owned objects are "
                     "created by the DDL)", WARN, "postgres cannot change them on Supabase: support ticket; until "
                     "then never install an extension into an API schema")]


# ---------------------------------------------------------------------------------------------------------------
# SOC tests
# ---------------------------------------------------------------------------------------------------------------

def check_definer_execute(ctx):
    return [_fail_if(_col(ctx, """
        SELECT format('%s (%s)', p.oid::regprocedure,
                      concat_ws(',', CASE WHEN has_function_privilege('anon', p.oid, 'EXECUTE') THEN 'anon' END,
                                     CASE WHEN has_function_privilege('authenticated', p.oid, 'EXECUTE') THEN 'authenticated' END))
          FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = ANY (string_to_array(current_setting('teos329.api_schemas'), ',')) AND p.prosecdef
           AND (has_function_privilege('anon', p.oid, 'EXECUTE')
                OR has_function_privilege('authenticated', p.oid, 'EXECUTE'))"""),
        "T4", "SECURITY DEFINER functions not executable by anon/authenticated")]


def check_search_path(ctx):
    definer = _col(ctx, """
        SELECT p.oid::regprocedure::text
          FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = ANY (string_to_array(current_setting('teos329.api_schemas'), ',')) AND p.prosecdef
           AND NOT coalesce('search_path=""' = ANY (p.proconfig), false)""")
    invoker = _col(ctx, """
        SELECT p.oid::regprocedure::text
          FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = ANY (string_to_array(current_setting('teos329.api_schemas'), ',')) AND NOT p.prosecdef
           AND NOT EXISTS (SELECT 1 FROM unnest(p.proconfig) c WHERE c LIKE 'search_path=%')
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d
                            WHERE d.classid = 'pg_catalog.pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')""")
    return [_fail_if(definer, "T5a", "SECURITY DEFINER functions pinned to search_path = ''"),
            _fail_if(invoker, "T5b", "SECURITY INVOKER functions with a fixed search_path", WARN,
                     "run with the caller's rights, so not an escalation; pin them when they are next changed")]


def check_extensions(ctx):
    return [_fail_if(_col(ctx, """
        SELECT format('%s (in %s)', e.extname, n.nspname)
          FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
         WHERE n.nspname = 'public' OR n.nspname = ANY (string_to_array(current_setting('teos329.api_schemas'), ','))"""),
        "T6", "no extension in public or another API schema")]


def check_auth(ctx):
    titles = {"T8a": "anonymous sign-ins disabled", "T8b": "self sign-up disabled",
              "T8c": "email auto-confirm disabled"}
    s = ctx.auth_settings
    if s is None:
        return [Result(i, t, UNKNOWN, note=ctx.auth_error) for i, t in titles.items()]
    external = s.get("external") if isinstance(s.get("external"), dict) else {}
    out = []
    anonymous = external.get("anonymous_users")
    if not isinstance(anonymous, bool):
        out.append(Result("T8a", titles["T8a"], UNKNOWN, note="external.anonymous_users missing from /auth/v1/settings"))
    else:
        out.append(Result("T8a", titles["T8a"], FAIL if anonymous else PASS, ["auth: anonymous_users"] if anonymous else []))
    for id_, key, bad_when, note in (("T8b", "disable_signup", False, SIGNUP_NOTE),
                                     ("T8c", "mailer_autoconfirm", True, AUTOCONFIRM_NOTE)):
        value = s.get(key)
        if not isinstance(value, bool):
            out.append(Result(id_, titles[id_], UNKNOWN, note=f"{key} missing from /auth/v1/settings"))
        elif value == bad_when:
            out.append(Result(id_, titles[id_], WARN, [f"auth: {key}"], note))
        else:
            out.append(Result(id_, titles[id_], PASS))
    return out


def check_storage(ctx):
    titles = ("T10a", "no public storage bucket"), ("T10b", "no storage policy for anon/PUBLIC, no authenticated write policy")
    if not _col(ctx, "SELECT 1 WHERE to_regclass('storage.buckets') IS NOT NULL"):
        return [Result(i, t, PASS, note="no storage schema") for i, t in titles]
    buckets = _col(ctx, "SELECT name FROM storage.buckets WHERE public")
    policies = _col(ctx, """
        SELECT format('storage.%s.%s (%s, to %s)', tablename, policyname, cmd, array_to_string(roles, ','))
          FROM pg_catalog.pg_policies
         WHERE schemaname = 'storage' AND tablename IN ('objects', 'buckets')
           AND (roles && ARRAY['anon', 'public']::name[]
                OR (roles && ARRAY['authenticated']::name[] AND cmd IN ('INSERT', 'UPDATE', 'DELETE', 'ALL')))""")
    return [_fail_if(buckets, *titles[0]), _fail_if(policies, *titles[1])]


#: pgaudit.* settings of postgres / authenticator, or of the whole database, from pg_db_role_setting (readable by
#: every role, unlike shared_preload_libraries)
AUDIT_SETTINGS_SQL = """
    SELECT format('%s: %s', CASE WHEN s.setrole = 0 THEN 'database' ELSE pg_catalog.pg_get_userbyid(s.setrole)::text END,
                  split_part(c, '=', 1))
      FROM pg_catalog.pg_db_role_setting s, unnest(s.setconfig) c
     WHERE (s.setrole = 0 OR pg_catalog.pg_get_userbyid(s.setrole) IN ('postgres', 'authenticator'))
       AND s.setdatabase IN (0, (SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()))
       AND c ~* {pattern}"""
AUDIT_ON = "'^pgaudit\\.(role=.|log=(?!none$).)'"
LOG_PARAMETER_ON = "'^pgaudit\\.log_parameter=(on|true|yes|1)$'"
AUDIT_TITLE = "pgaudit installed, preloaded and configured"


def check_audit(ctx):
    installed = bool(_col(ctx, "SELECT 1 FROM pg_catalog.pg_extension WHERE extname = 'pgaudit'"))
    # pg_settings leaves out shared_preload_libraries for a role without pg_read_all_settings (current_setting would
    # raise and abort the transaction). On Supabase postgres cannot grant that role, so a dedicated login cannot read it.
    preload = _col(ctx, "SELECT setting FROM pg_catalog.pg_settings WHERE name = 'shared_preload_libraries'")
    settings = sorted(set(_col(ctx, AUDIT_SETTINGS_SQL.replace("{pattern}", AUDIT_ON))
                          + _col(ctx, "SELECT 'server: pgaudit.log' WHERE lower(coalesce(current_setting('pgaudit.log', "
                                      "true), 'none')) NOT IN ('', 'none')")))
    parameters = _col(ctx, AUDIT_SETTINGS_SQL.replace("{pattern}", LOG_PARAMETER_ON))
    missing = [m for m, ok in (("pgaudit extension not installed", installed),
                               ("no pgaudit.log / pgaudit.role setting", bool(settings))) if not ok]
    if preload and "pgaudit" not in preload[0]:
        missing.insert(1, "pgaudit not in shared_preload_libraries")
    if missing or parameters:
        audit = Result("T11a", AUDIT_TITLE, FAIL, missing + parameters,
                       "pgaudit.log_parameter on writes OTP codes and tokens to the logs" if parameters else "")
    elif not preload:
        audit = Result("T11a", AUDIT_TITLE, UNKNOWN, settings,
                       f"shared_preload_libraries is not readable by {_col(ctx, 'SELECT current_user')[0]} (needs "
                       "pg_read_all_settings, which postgres cannot grant on Supabase): pgaudit is installed and "
                       "configured, but whether it is preloaded is not verified. Run as postgres to settle it")
    else:
        audit = Result("T11a", AUDIT_TITLE, PASS, settings)
    if ctx.log_drain:
        drain = Result("T11b", "log drain configured", PASS, [f"log drain: {ctx.log_drain}"], "attested by the operator")
    else:
        drain = Result("T11b", "log drain configured", UNKNOWN,
                       note="not readable from the database: check Project Settings -> Log Drains, then pass --log-drain NAME")
    return [audit, drain]


def check_version(ctx):
    num = int(_col(ctx, "SELECT current_setting('server_version_num')")[0])
    major = num // 10000
    version = _col(ctx, "SELECT current_setting('server_version')")[0].split(" ")[0]
    ok = major >= ctx.min_pg_major
    return [Result("T12", f"PostgreSQL major version >= {ctx.min_pg_major}", PASS if ok else FAIL,
                   [] if ok else [f"PostgreSQL {version}"], "" if ok else "upgrade the project (Settings -> Infrastructure)")]


CHECKS = (check_rls, check_open_policies, check_anon_grants, check_authenticated_writes, check_anon_execute,
          check_default_privileges, check_definer_execute, check_search_path, check_extensions, check_auth,
          check_storage, check_audit, check_version)


# ---------------------------------------------------------------------------------------------------------------
# Appendix A.1 exposure line: our approximation of the SOC query (the same 8 counts as the A.1 report of the
# TEOS-323 hardening script teos323_harden.sql); the SOC query itself is not in this repository
# ---------------------------------------------------------------------------------------------------------------

A1_SQL = """
WITH rel AS (SELECT oid, relkind, relrowsecurity FROM pg_catalog.pg_class WHERE relnamespace = 'public'::regnamespace),
     fn AS (SELECT oid, prosecdef FROM pg_catalog.pg_proc WHERE pronamespace = 'public'::regnamespace)
SELECT
  (SELECT count(*) FROM rel WHERE relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
      AND (has_table_privilege('anon', oid, 'SELECT')
           OR (relkind <> 'S' AND has_any_column_privilege('anon', oid, 'SELECT')))),
  (SELECT count(*) FROM fn WHERE prosecdef AND has_function_privilege('anon', oid, 'EXECUTE')),
  (SELECT count(*) FROM rel WHERE relkind IN ('r', 'p') AND NOT relrowsecurity),
  (SELECT count(*) FROM rel WHERE relkind IN ('r', 'p', 'v', 'm', 'f')
      AND has_table_privilege('anon', oid, 'INSERT,UPDATE,DELETE,TRUNCATE')),
  (SELECT count(*) FROM fn WHERE has_function_privilege('anon', oid, 'EXECUTE')),
  (SELECT count(*) FROM pg_catalog.pg_policies WHERE schemaname = 'public' AND roles && ARRAY['anon', 'public']::name[]),
  (SELECT count(*) FROM pg_catalog.pg_policies WHERE schemaname = 'storage' AND cmd IN ('INSERT', 'UPDATE', 'DELETE', 'ALL')
      AND roles && ARRAY['anon', 'public', 'authenticated']::name[]),
  (SELECT count(*) FROM pg_catalog.pg_default_acl d, aclexplode(d.defaclacl) a
    WHERE d.defaclrole = 'postgres'::regrole AND d.defaclnamespace IN (0, 'public'::regnamespace)
      AND a.grantee IN (0, 'anon'::regrole) AND NOT (d.defaclnamespace = 0 AND d.defaclobjtype IN ('T', 'n')))
"""
A1_ITEMS = ("anon_read", "secdef_anon", "rls_off", "anon_write", "anon_execute", "anon_policies", "storage_write",
            "anon_default_privileges")


def a1_line(query) -> str:
    counts = query(A1_SQL)[0]
    return "A.1 exposure: " + " ".join(f"{k}={v}" for k, v in zip(A1_ITEMS, counts)) + f" total={sum(counts)}"


# ---------------------------------------------------------------------------------------------------------------
# accepted risks, verdict, output
# ---------------------------------------------------------------------------------------------------------------

def parse_accept(values: Iterable[str]) -> dict:
    """``['T10a:media=public images, SOC 2026-11-20', 'T8a=...']`` -> {('T10a', 'media'): reason, ('T8a', None): reason}."""
    accepted = {}
    for value in values:
        target, sep, reason = value.partition("=")
        if not sep or not reason.strip() or not target.strip():
            raise ValueError(f"--accept needs ID[:OBJECT]=REASON, got {target.strip() or '(empty)'}")
        check_id, _, obj = target.strip().partition(":")
        accepted[(check_id, obj or None)] = reason.strip()
    return accepted


def _object_name(obj: str) -> str:
    """'storage.objects.p (ALL, to anon)' -> 'storage.objects.p'; bucket names stay as they are."""
    return obj.split(" (", 1)[0]


def apply_accepted(results: list, accepted: dict) -> list:
    for r in results:
        if r.status != FAIL:
            continue
        if (r.id, None) in accepted:
            r.status, r.note = ACCEPTED, f"accepted: {accepted[(r.id, None)]}"
            continue
        reasons = {_object_name(o): accepted.get((r.id, _object_name(o))) for o in r.objects}
        if r.objects and all(reasons.values()):
            r.status = ACCEPTED
            r.note = "accepted: " + "; ".join(f"{o}: {why}" for o, why in reasons.items())
        elif any(reasons.values()):
            r.objects = [o for o in r.objects if not reasons[_object_name(o)]]
            r.note = (r.note + "; " if r.note else "") + "accepted for: " + ", ".join(o for o, why in reasons.items() if why)
    return results


def exit_code(results: list) -> int:
    statuses = {r.status for r in results}
    if FAIL in statuses:
        return EXIT_FAIL
    if UNKNOWN in statuses:
        return EXIT_UNKNOWN
    return EXIT_OK


def render(results: list, header: list, a1: str, code: int) -> str:
    lines = list(header)
    for r in results:
        lines.append(f"{r.id:<5} {r.status:<8} {r.title}")
        lines.extend(f"        - {o}" for o in r.objects)
        if r.note:
            lines.append(f"        note: {r.note}")
    lines.append(a1)
    counts = {s: sum(r.status == s for r in results) for s in (PASS, FAIL, WARN, ACCEPTED, UNKNOWN)}
    verdict = {EXIT_OK: "PASS", EXIT_FAIL: "FAIL", EXIT_UNKNOWN: "INCOMPLETE (UNKNOWN checks)"}[code]
    lines.append(f"Result: {verdict}  " + ", ".join(f"{n} {s}" for s, n in counts.items()) + f"  (exit {code})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------------------------------------------

def read_auth_settings(url: str, key: str, get=None):
    """(settings, '') or (None, reason). The response body and the key are never printed."""
    if not url or not key:
        return None, "SUPABASE_URL and SUPABASE_ANON_KEY (or SUPABASE_KEY) not set: Auth settings not read"
    if get is None:
        import httpx
        get = httpx.get
    try:
        response = get(f"{url.rstrip('/')}/auth/v1/settings", headers={"apikey": key}, timeout=15)
    except Exception as exc:  # network errors: report the type only (messages can carry the URL)
        return None, f"GET /auth/v1/settings failed: {type(exc).__name__}"
    if response.status_code != 200:
        return None, f"GET /auth/v1/settings returned HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return None, "GET /auth/v1/settings did not return JSON"
    return (body, "") if isinstance(body, dict) else (None, "GET /auth/v1/settings returned no object")


def connect(url: str):
    import psycopg
    conn = psycopg.connect(url, connect_timeout=15, application_name="teos329_security_check")
    conn.read_only = True   # every transaction is BEGIN READ ONLY: a write would fail
    return conn


def run(argv=None, environ=None, connect_fn=connect, get=None, out=sys.stdout) -> int:
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--database-url-env", default="SECURITY_CHECK_DATABASE_URL")
    parser.add_argument("--log-drain")
    parser.add_argument("--min-pg-major", type=int)
    parser.add_argument("--schema", action="append", dest="schemas")
    parser.add_argument("--accept", action="append", default=[])
    try:
        default_major = int(environ.get("SECURITY_CHECK_MIN_PG_MAJOR") or DEFAULT_MIN_PG_MAJOR)
        args = parser.parse_args(argv)
        args.min_pg_major = args.min_pg_major or default_major
        accepted = parse_accept(args.accept)
    except SystemExit as exc:
        return EXIT_ERROR if exc.code else EXIT_OK
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    db_url = environ.get(args.database_url_env, "")
    if not db_url:
        print(f"error: {args.database_url_env} is not set", file=sys.stderr)
        return EXIT_ERROR
    try:
        conn = connect_fn(db_url)
    except Exception as exc:  # the message can contain the host or user: type only
        print(f"error: cannot connect to the database ({type(exc).__name__})", file=sys.stderr)
        return EXIT_ERROR

    anon_key_name = "SUPABASE_ANON_KEY" if environ.get("SUPABASE_ANON_KEY") else "SUPABASE_KEY"
    settings, auth_error = read_auth_settings(environ.get("SUPABASE_URL", ""), environ.get(anon_key_name, ""), get)
    with conn:
        def query(sql, params=()):
            return conn.execute(sql, params or None).fetchall()

        missing = [r for r in ("anon", "authenticated", "service_role")
                   if not query("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (r,))]
        if missing:
            print(f"error: not a Supabase database (role {', '.join(missing)} missing)", file=sys.stderr)
            return EXIT_ERROR
        query("SELECT set_config('teos329.api_schemas', %s, true)", (",".join(args.schemas or ["public"]),))
        ctx = Context(query, settings, auth_error, args.log_drain, args.min_pg_major, tuple(args.schemas or ["public"]))
        results = [r for check in CHECKS for r in check(ctx)]
        a1 = a1_line(query)
        database, version = query("SELECT current_database(), current_setting('server_version')")[0]
        conn.rollback()

    results = apply_accepted(results, accepted)
    code = exit_code(results)
    header = [
        f"TEOS-329 security check  {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
        f"database: {database}  PostgreSQL {version.split(' ')[0]}  API schemas: {', '.join(ctx.schemas)}",
        f"inputs: {args.database_url_env}=set  SUPABASE_URL={'set' if environ.get('SUPABASE_URL') else 'not set'}  "
        f"{anon_key_name}={'set' if environ.get(anon_key_name) else 'not set'}  "
        f"log drain={'attested' if args.log_drain else 'not given'}",
        "",
    ]
    print(render(results, header, a1, code), file=out)
    return code


if __name__ == "__main__":
    sys.exit(run())
