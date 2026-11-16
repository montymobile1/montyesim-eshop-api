"""TEOS-271: the per-user tables stay locked down in the schema files.

Read from supabase_ddl.sql, the only schema file (fresh environments load it whole, existing ones
run its RLS section): every per-user table has ROW LEVEL SECURITY enabled and forced, the owned
tables have their ``FOR SELECT TO authenticated`` policy on the owner column, the tables the old
admin portal reads have the admin policy (``app_metadata.role``), there is no other policy, and
nothing later in the file disables it again. A future table edit cannot silently drop the lock-down.

The SQL is read with comments and quoted bodies removed, the way PostgreSQL reads it: block comments
nest, so a ``/*`` inside a comment opens another one. Such a comment once hid the end of
supabase_ddl.sql (everything after it was never run); the scanner fails on it.
"""
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DDL = ROOT / "supabase_ddl.sql"

UID = "(select auth.uid())"
#: table -> (policy name, USING expression) of its only policy
OWNED = {
    "user_profile": ("user_profile_select_own", f"user_id = {UID}"),
    "user_profile_bundle": ("user_profile_bundle_select_own", f"user_id = {UID}"),
    "user_order": ("user_order_select_own", f"user_id = {UID} or anonymous_user_id = {UID}"),
    "user_wallet": ("user_wallet_select_own", f"user_id = {UID}"),
    "user_wallet_transaction": ("user_wallet_transaction_select_own",
                                f"wallet_id in (select w.id from public.user_wallet w where w.user_id = {UID})"),
    "promotion_usage": ("promotion_usage_select_own", f"user_id = {UID}"),
    "device": ("device_select_own", f"user_id = {UID}"),
    "notification": ("notification_select_own", f"user_id = {UID}"),
}
ADMIN_USING = "((select auth.jwt()) -> 'app_metadata' ->> 'role') in ('admin', 'super_admin')"
#: tables the old admin portal reads with the admin's JWT: second policy, for portal admins
ADMIN_READ = ("user_order", "user_wallet", "user_wallet_transaction", "promotion_usage", "device",
              "users_copy", "contact_us")
#: no owner column: no policy for users (users_copy and contact_us: the admin policy only)
NO_POLICY = ("user_otp", "users_copy", "contact_us")
ALL_TABLES = (*OWNED, *NO_POLICY)


def expected_policies(table):
    policies = {}
    if table in OWNED:
        name, using = OWNED[table]
        policies[name] = ("select", "authenticated", using, "")
    if table in ADMIN_READ:
        policies[f"{table}_select_admin"] = ("select", "authenticated", ADMIN_USING, "")
    return policies


def statements(sql: str):
    """The statements of ``sql``, lower-cased and on one line, without comments or the bodies of
    dollar quotes (a function body cannot create or drop a policy here)."""
    out, i, n = [], 0, len(sql)
    while i < n:
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end
        elif sql.startswith("/*", i):
            depth, i = 1, i + 2
            while depth:
                if i >= n:
                    raise AssertionError("unterminated /* comment: the rest of the file would never run")
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        elif sql[i] == "'":
            end = i + 1
            while True:
                end = sql.find("'", end)
                assert end >= 0, "unterminated string"
                if sql.startswith("''", end):
                    end += 2
                    continue
                break
            out.append(sql[i:end + 1].replace(";", "\0"))  # kept: policies compare their literals
            i = end + 1
        elif sql[i] == "$" and (tag := re.match(r"\$[A-Za-z_]*\$", sql[i:])):
            end = sql.find(tag.group(0), i + len(tag.group(0)))
            assert end >= 0, f"unterminated dollar quote {tag.group(0)}"
            out.append("$$")
            i = end + len(tag.group(0))
        else:
            out.append(sql[i])
            i += 1
    return [re.sub(r"\s+", " ", s).strip().lower() for s in "".join(out).split(";") if s.strip()]


def _norm(expr: str) -> str:
    expr = re.sub(r"\s+", " ", expr).strip()
    expr = re.sub(r"\(\s+", "(", expr)
    return re.sub(r"\s+\)", ")", expr)


def rls_state(sql: str):
    """table -> {"enabled", "forced", "policies": {name: (command, roles, using)}} after the file ran."""
    state = {t: {"enabled": False, "forced": False, "policies": {}} for t in ALL_TABLES}
    table = r"(?:public\.)?([a-z_]+)"
    for stmt in statements(sql):
        if m := re.fullmatch(rf"alter table (?:only )?(?:if exists )?{table} (enable|disable) row level security", stmt):
            if m.group(1) in state:
                state[m.group(1)]["enabled"] = m.group(2) == "enable"
        elif m := re.fullmatch(rf"alter table (?:only )?(?:if exists )?{table} (no )?force row level security", stmt):
            if m.group(1) in state:
                state[m.group(1)]["forced"] = not m.group(2)
        elif m := re.fullmatch(rf"drop policy (?:if exists )?([a-z_]+) on {table}", stmt):
            if m.group(2) in state:
                state[m.group(2)]["policies"].pop(m.group(1), None)
        elif m := re.fullmatch(rf"create policy ([a-z_]+) on {table}(?: as (?:permissive|restrictive))?"
                               rf"(?: for (all|select|insert|update|delete))?(?: to ([a-z_, ]+?))?"
                               rf"(?: using \((.*)\))?(?: with check \((.*)\))?", stmt):
            name, tbl, command, roles, using, check = m.groups()
            if tbl in state:
                state[tbl]["policies"][name] = (command or "all", (roles or "public").replace(" ", ""),
                                                _norm(using or ""), _norm(check or ""))
        elif (m := re.match(r'create policy (?:"[^"]*"|\S+) on ("?[a-z_]+"?\.)?"?([a-z_]+)"?', stmt)) \
                and m.group(1) in (None, "public.", '"public".') and m.group(2) in state:
            raise AssertionError(f"policy statement not understood by this test: {stmt}")
    return state


@pytest.fixture(scope="module")
def state():
    return rls_state(DDL.read_text())


@pytest.mark.parametrize("table", ALL_TABLES)
def test_rls_is_enabled_and_forced(state, table):
    assert state[table]["enabled"], f"{table}: ROW LEVEL SECURITY is not enabled"
    assert state[table]["forced"], f"{table}: ROW LEVEL SECURITY is not forced"


@pytest.mark.parametrize("table", ALL_TABLES)
def test_table_has_exactly_its_select_policies(state, table):
    assert state[table]["policies"] == expected_policies(table)


def test_user_otp_has_no_policy(state):
    assert state["user_otp"]["policies"] == {}


def test_no_policy_lets_anyone_write(state):
    assert all(policy[0] == "select" for table in ALL_TABLES for policy in state[table]["policies"].values())


def test_supabase_ddl_has_no_unterminated_comment():
    statements(DDL.read_text())  # raises on an unterminated (nested) block comment


def test_scanner_sees_a_nested_comment_like_postgres():
    with pytest.raises(AssertionError, match="unterminated"):
        statements("/* see procedures/*.sql */ alter table user_order enable row level security;")
    assert statements("/* a /* b */ c */ select 1; -- x\nselect $f$ ; $f$;") == ["select 1", "select $$"]


def test_a_later_disable_or_drop_is_caught():
    sql = DDL.read_text() + """
        ALTER TABLE public.device DISABLE ROW LEVEL SECURITY;
        ALTER TABLE public.notification NO FORCE ROW LEVEL SECURITY;
        DROP POLICY user_order_select_own ON public.user_order;
        CREATE POLICY device_insert ON public.device FOR INSERT TO authenticated WITH CHECK (true);
    """
    state = rls_state(sql)
    assert not state["device"]["enabled"]
    assert not state["notification"]["forced"]
    assert "user_order_select_own" not in state["user_order"]["policies"]
    assert "device_insert" in state["device"]["policies"]


# ---------- TEOS-326: SECURITY DEFINER functions ----------
#
# Replayed from the statements of supabase_ddl.sql, in order: which functions are SECURITY DEFINER at the
# end, their SET search_path, who may EXECUTE them (a new function starts executable by PUBLIC, anon,
# authenticated and service_role: PostgreSQL's default plus Supabase's default privileges) and their COMMENT.

#: roles that must never execute a SECURITY DEFINER function
CLOSED_TO = {"public", "anon", "authenticated"}
_TYPE_ALIASES = {"int": "integer", "int4": "integer", "int8": "bigint", "bool": "boolean",
                 "timestamp without time zone": "timestamp", "character varying": "varchar"}


def _split_top(text: str):
    """``text`` split on commas outside parentheses."""
    parts, depth, cur = [], 0, ""
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return [p.strip() for p in parts + [cur] if p.strip()]


def _arg_types(args: str):
    """'p_a text, p_b integer default 0, uuid[]' -> ('text', 'integer', 'uuid[]')."""
    types = []
    for arg in _split_top(re.sub(r"\s+", " ", args.lower())):
        arg = re.split(r"\s+default\s+|\s*=\s*", arg)[0].strip()
        arg = re.sub(r"^(in|variadic)\s+", "", arg)
        words = arg.split(" ")
        if len(words) > 1 and re.fullmatch(r"[a-z_][a-z0-9_]*", words[0]) \
                and words[0] not in ("double", "character", "timestamp", "time", "bit"):
            arg = " ".join(words[1:])  # drop the parameter name
        types.append(_TYPE_ALIASES.get(arg, arg))
    return tuple(types)


_SIG = r"(?:public\.)?([a-z_]+)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)"


def definer_state(sql: str):
    """(name, arg types) -> {"definer", "search_path", "execute": roles, "comment"} after the file ran."""
    functions = {}
    for stmt in statements(sql):
        if m := re.match(rf"create (or replace )?function {_SIG}", stmt):
            key = (m.group(2), _arg_types(m.group(3)))
            header = stmt.replace("$$", " ")
            path = re.search(r"\bset search_path\s*(?:=|to)\s*('[^']*'|[a-z_\"$, ]+?)(?=\s+(?:as|language|security|"
                             r"stable|volatile|immutable|returns|set|cost|parallel|strict)\b|\s*$)", header)
            previous = functions.get(key)
            functions[key] = {
                "definer": bool(re.search(r"\bsecurity definer\b", header)),
                "search_path": path.group(1).strip() if path else None,
                # CREATE OR REPLACE keeps the privileges and the comment of an existing function
                "execute": previous["execute"] if previous else {"public", "anon", "authenticated", "service_role"},
                "comment": previous["comment"] if previous else None,
            }
        elif m := re.fullmatch(rf"drop function (?:if exists )?{_SIG}(?: cascade| restrict)?", stmt):
            functions.pop((m.group(1), _arg_types(m.group(2))), None)
        elif m := re.fullmatch(rf"(grant|revoke) (?:all|execute)(?: privileges)? on function {_SIG} (?:to|from) ([a-z_, ]+)", stmt):
            fn = functions.get((m.group(2), _arg_types(m.group(3))))
            if fn:
                roles = {r.strip() for r in m.group(4).split(",")}
                fn["execute"] = fn["execute"] | roles if m.group(1) == "grant" else fn["execute"] - roles
        elif m := re.fullmatch(rf"alter function {_SIG} (set search_path\s*(?:=|to)\s*(.+)|reset search_path|reset all)", stmt):
            fn = functions.get((m.group(1), _arg_types(m.group(2))))
            if fn:
                fn["search_path"] = m.group(4).strip() if m.group(4) else None
        elif m := re.fullmatch(rf"comment on function {_SIG} is (.+)", stmt, flags=re.S):
            fn = functions.get((m.group(1), _arg_types(m.group(2))))
            if fn:
                fn["comment"] = None if m.group(3).strip() == "null" else m.group(3)
    return {key: fn for key, fn in functions.items() if fn["definer"]}


@pytest.fixture(scope="module")
def definers():
    return definer_state(DDL.read_text())


def _label(key):
    return f"{key[0]}({', '.join(key[1])})"


def test_the_ddl_has_its_security_definer_functions(definers):
    names = {name for name, _ in definers}
    assert {"sync_users_copy", "search_bundle", "search_bundles", "admin_insert_audited",
            "admin_revoke_user_sessions"} <= names
    assert len(definers) >= 33


def test_every_security_definer_function_pins_an_empty_search_path(definers):
    bad = {_label(k): fn["search_path"] for k, fn in definers.items() if fn["search_path"] not in ("''", '""')}
    assert not bad, f"SECURITY DEFINER without SET search_path = '': {bad}"


def test_no_security_definer_function_is_executable_by_public_anon_or_authenticated(definers):
    bad = {_label(k): sorted(fn["execute"] & CLOSED_TO) for k, fn in definers.items() if fn["execute"] & CLOSED_TO}
    assert not bad, f"REVOKE ALL ON FUNCTION ... FROM PUBLIC, anon, authenticated is missing for {bad}"


def test_service_role_executes_every_security_definer_function(definers):
    bad = [_label(k) for k, fn in definers.items() if "service_role" not in fn["execute"]]
    assert not bad, f"GRANT EXECUTE ... TO service_role is missing for {bad}"


def test_every_security_definer_function_says_why_it_is_granted(definers):
    bad = [_label(k) for k, fn in definers.items() if not fn["comment"]]
    assert not bad, f"COMMENT ON FUNCTION (the reason for its grants) is missing for {bad}"


def test_triggers_fired_by_admin_writes_are_pinned_too():
    """promotion / promotion_rule triggers fire inside admin_insert/update_audited and would inherit their
    empty search_path: they must not depend on the caller's search_path either."""
    sql = DDL.read_text()
    for name in ("validate_bundle_codes", "update_promotion_rule_name"):
        stmt = next(s for s in statements(sql) if re.match(rf"create (or replace )?function (public\.)?{name}\(", s))
        assert "set search_path = ''" in stmt, name


def test_new_postgres_functions_are_not_executable_by_public():
    stmts = statements(DDL.read_text())
    assert "alter default privileges for role postgres revoke execute on functions from public" in stmts
    assert ("alter default privileges for role postgres in schema public revoke execute on functions "
            "from anon, authenticated") in stmts


def test_a_later_grant_or_unpinned_definer_is_caught():
    sql = DDL.read_text() + """
        GRANT EXECUTE ON FUNCTION public.admin_end_session(uuid) TO anon;
        ALTER FUNCTION public.admin_lookup_admins(text, integer, integer, uuid) RESET search_path;
        CREATE FUNCTION public.leaky(p_id uuid) RETURNS int LANGUAGE sql SECURITY DEFINER AS $$ SELECT 1 $$;
    """
    state = definer_state(sql)
    assert "anon" in state[("admin_end_session", ("uuid",))]["execute"]
    assert state[("admin_lookup_admins", ("text", "integer", "integer", "uuid"))]["search_path"] is None
    leaky = state[("leaky", ("uuid",))]
    assert leaky["search_path"] is None and "public" in leaky["execute"] and leaky["comment"] is None


def test_signature_parsing():
    assert _arg_types("p_search_term text, p_page integer DEFAULT 0, p_tag_ids uuid[] DEFAULT NULL ::uuid[]") == \
           ("text", "integer", "uuid[]")
    assert _arg_types("text, integer, timestamp, numeric(10, 2)") == ("text", "integer", "timestamp", "numeric(10, 2)")
    assert _arg_types("p_from timestamp without time zone default null") == ("timestamp",)


# ---------- TEOS-326, live: the catalogue of a database loaded with supabase_ddl.sql ----------
# DDL_TEST_DATABASE_URL: a disposable database with the Supabase roles (tests/rls/supabase_stub.py loads
# one). Never point it at a real environment: it only reads, but it is meant for the freshly loaded DDL.

DDL_DATABASE_URL = os.getenv("DDL_TEST_DATABASE_URL", "")
live = pytest.mark.skipif(not DDL_DATABASE_URL, reason="DDL_TEST_DATABASE_URL not set")


def _query(sql, *params):
    import psycopg
    with psycopg.connect(DDL_DATABASE_URL) as conn:
        return conn.execute(sql, params or None).fetchall()


@live
def test_live_no_security_definer_function_is_executable_by_anon_or_public():
    rows = _query("""
        SELECT p.oid::regprocedure::text
          FROM pg_proc p
         WHERE p.pronamespace = 'public'::regnamespace AND p.prosecdef
           AND (has_function_privilege('anon', p.oid, 'EXECUTE')
                OR has_function_privilege('authenticated', p.oid, 'EXECUTE')
                OR EXISTS (SELECT 1 FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
                            WHERE a.grantee = 0))""")
    assert rows == [], f"SECURITY DEFINER functions executable by anon/authenticated/PUBLIC: {rows}"


@live
def test_live_every_security_definer_function_has_a_search_path():
    rows = _query("""
        SELECT p.oid::regprocedure::text FROM pg_proc p
         WHERE p.pronamespace = 'public'::regnamespace AND p.prosecdef
           AND NOT EXISTS (SELECT 1 FROM unnest(p.proconfig) c WHERE c LIKE 'search_path=%')""")
    assert rows == [], f"SECURITY DEFINER functions without search_path= in proconfig: {rows}"


@live
def test_live_service_role_executes_every_function_in_public():
    rows = _query("""SELECT p.oid::regprocedure::text FROM pg_proc p
                      WHERE p.pronamespace = 'public'::regnamespace
                        AND NOT has_function_privilege('service_role', p.oid, 'EXECUTE')""")
    assert rows == [], f"the API (service_role) cannot execute: {rows}"


@live
def test_live_definer_bodies_compile_with_an_empty_search_path():
    """plpgsql_check, when installed: every statement of every SECURITY DEFINER function (and of the triggers
    they fire) resolves with search_path = '' -- an unqualified name would fail here, not in production."""
    if not _query("SELECT 1 FROM pg_available_extensions WHERE name = 'plpgsql_check'"):
        pytest.skip("plpgsql_check is not available on this server")
    import psycopg
    with psycopg.connect(DDL_DATABASE_URL) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS plpgsql_check")
        conn.execute("SET search_path = ''")
        rows = conn.execute("""
            SELECT p.oid::regprocedure::text, r.lineno, r.message
              FROM pg_catalog.pg_proc p
              JOIN pg_catalog.pg_language l ON l.oid = p.prolang AND l.lanname = 'plpgsql'
              LEFT JOIN LATERAL (SELECT t.tgrelid FROM pg_catalog.pg_trigger t WHERE t.tgfoid = p.oid LIMIT 1) t ON true
             CROSS JOIN LATERAL plpgsql_check_function_tb(p.oid, coalesce(t.tgrelid, 0), fatal_errors => false,
                       other_warnings => false, extra_warnings => false, performance_warnings => false) r
             WHERE p.pronamespace = 'public'::regnamespace
               AND (p.prosecdef OR p.proname IN ('validate_bundle_codes', 'update_promotion_rule_name'))
               AND r.level = 'error'""".replace("plpgsql_check_function_tb",
                                                 f"{_plpgsql_check_schema(conn)}.plpgsql_check_function_tb")).fetchall()
        conn.rollback()
    assert rows == [], f"unqualified references under search_path '': {rows}"


def _plpgsql_check_schema(conn):
    return conn.execute("SELECT n.nspname FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n "
                        "ON n.oid = e.extnamespace WHERE e.extname = 'plpgsql_check'").fetchone()[0]
