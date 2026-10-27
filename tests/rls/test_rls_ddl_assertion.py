"""TEOS-271: the per-user tables stay locked down in the schema files.

Read from supabase_ddl.sql (a fresh environment) and from the migration under db/rls_user_tables
(existing environments): every per-user table has ROW LEVEL SECURITY enabled and forced, the owned
tables have their ``FOR SELECT TO authenticated`` policy on the owner column, the tables the old
admin portal reads have the admin policy (``app_metadata.role``), there is no other policy, and
nothing later in the file disables it again. A future table edit cannot silently drop the lock-down.

The SQL is read with comments and quoted bodies removed, the way PostgreSQL reads it: block comments
nest, so a ``/*`` inside a comment opens another one. Such a comment once hid the end of
supabase_ddl.sql (everything after it was never run); the scanner fails on it.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DDL = ROOT / "supabase_ddl.sql"
MIGRATION_UP = ROOT / "db" / "rls_user_tables" / "001_rls_user_tables.up.sql"
MIGRATION_DOWN = ROOT / "db" / "rls_user_tables" / "001_rls_user_tables.down.sql"

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


@pytest.fixture(scope="module", params=[DDL, MIGRATION_UP], ids=["supabase_ddl", "migration"])
def state(request):
    return rls_state(request.param.read_text())


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
    sql = MIGRATION_UP.read_text() + """
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


def test_down_migration_undoes_the_up_migration():
    state = rls_state(MIGRATION_UP.read_text() + "\n" + MIGRATION_DOWN.read_text())
    assert all(not s["enabled"] and not s["forced"] and not s["policies"] for s in state.values())
