"""TEOS-329: scripts/security_check.py.

Offline: verdict, accepted risks, Auth settings (T8) and the output never carrying a key value.
Live (SECURITY_CHECK_TEST_DATABASE_URL, a disposable database loaded by tests/rls/supabase_stub.py on a server with
pgaudit preloaded, as in .github/workflows/security-check.yml):
  * AC1: the database loaded from supabase_ddl.sql passes every check with no manual step;
  * AC2: each exposure of the story, added on its own (in a transaction that is rolled back), makes it fail;
  * AC3: the script only runs SELECTs in a READ ONLY transaction and prints no value.
"""
import io
import os
from types import SimpleNamespace

import pytest

from scripts import security_check as sc

CLEAN_AUTH = {"external": {"anonymous_users": False, "email": True}, "disable_signup": True,
              "mailer_autoconfirm": False}


def fake_get(body=CLEAN_AUTH, status=200, calls=None):
    def get(url, headers=None, timeout=None):
        if calls is not None:
            calls.append((url, headers))
        return SimpleNamespace(status_code=status, json=lambda: body)
    return get


def auth_results(settings, error=""):
    ctx = sc.Context(query=None, auth_settings=settings, auth_error=error, log_drain=None, min_pg_major=17,
                     schemas=("public",))
    return {r.id: r for r in sc.check_auth(ctx)}


# ---------- offline ----------

def test_anonymous_sign_in_fails_and_signup_and_autoconfirm_only_warn():
    r = auth_results({"external": {"anonymous_users": True}, "disable_signup": False, "mailer_autoconfirm": True})
    assert (r["T8a"].status, r["T8b"].status, r["T8c"].status) == (sc.FAIL, sc.WARN, sc.WARN)
    assert "sign_in_with_otp" in r["T8b"].note and "should_create_user" in r["T8b"].note
    assert sc.exit_code(list(r.values())) == sc.EXIT_FAIL


def test_clean_auth_settings_pass():
    assert {r.status for r in auth_results(CLEAN_AUTH).values()} == {sc.PASS}


def test_auth_settings_not_read_are_unknown_not_pass():
    r = auth_results(None, "not set")
    assert {x.status for x in r.values()} == {sc.UNKNOWN}
    r = auth_results({"external": {}, "disable_signup": True})
    assert r["T8a"].status == sc.UNKNOWN and r["T8c"].status == sc.UNKNOWN and r["T8b"].status == sc.PASS


def test_read_auth_settings_sends_the_key_only_as_header_and_reports_no_body():
    calls = []
    body, err = sc.read_auth_settings("https://x.supabase.co/", "k-123", fake_get(calls=calls))
    assert body == CLEAN_AUTH and err == ""
    assert calls == [("https://x.supabase.co/auth/v1/settings", {"apikey": "k-123"})]
    assert sc.read_auth_settings("https://x", "k", fake_get(status=401)) == (None, "GET /auth/v1/settings returned HTTP 401")
    assert sc.read_auth_settings("", "k")[0] is None


def test_exit_codes():
    r = lambda s: sc.Result("X", "x", s)
    assert sc.exit_code([r(sc.PASS), r(sc.WARN), r(sc.ACCEPTED)]) == sc.EXIT_OK
    assert sc.exit_code([r(sc.PASS), r(sc.UNKNOWN)]) == sc.EXIT_UNKNOWN
    assert sc.exit_code([r(sc.UNKNOWN), r(sc.FAIL)]) == sc.EXIT_FAIL


def test_accepted_risk_for_a_check_or_for_named_objects_only():
    accepted = sc.parse_accept(["T8a=SOC accepted 2026-11-20", "T10a:media=public catalogue images"])
    results = sc.apply_accepted([sc.Result("T8a", "anon", sc.FAIL, ["auth: anonymous_users"]),
                                 sc.Result("T10a", "buckets", sc.FAIL, ["media"]),
                                 sc.Result("T10b", "policies", sc.FAIL, ["storage.objects.p (ALL, to anon)"])],
                                accepted)
    assert [r.status for r in results] == [sc.ACCEPTED, sc.ACCEPTED, sc.FAIL]
    assert "SOC accepted" in results[0].note
    partly = sc.apply_accepted([sc.Result("T10a", "buckets", sc.FAIL, ["media", "uploads"])], accepted)[0]
    assert partly.status == sc.FAIL and partly.objects == ["uploads"] and "media" in partly.note


@pytest.mark.parametrize("bad", ["T8a", "T8a=", "=reason"])
def test_accept_needs_an_id_and_a_reason(bad):
    with pytest.raises(ValueError):
        sc.parse_accept([bad])


def test_no_database_url_is_exit_2(capsys):
    assert sc.run([], environ={}) == sc.EXIT_ERROR
    assert "SECURITY_CHECK_DATABASE_URL is not set" in capsys.readouterr().err


def test_connection_error_prints_no_url(capsys):
    def refuse(url):
        raise OSError(f"could not connect to {url}")
    assert sc.run([], environ={"SECURITY_CHECK_DATABASE_URL": "postgresql://u:pw-secret@h/db"},
                  connect_fn=refuse) == sc.EXIT_ERROR
    assert "pw-secret" not in capsys.readouterr().err


def test_the_teos329_sweep_is_the_last_section_of_the_ddl():
    """Anything after the sweep would not be swept: a grant added below it would ship to every new instance."""
    from tests.rls.test_rls_ddl_assertion import DDL, statements
    sql = DDL.read_text()
    marker = sql.index("TEOS-329: hardening baseline sweep. KEEP THIS THE LAST SECTION OF THE FILE.")
    assert statements(sql[marker:].split("\n", 1)[1]) == ["do $$"]


# ---------- live ----------

DATABASE_URL = os.getenv("SECURITY_CHECK_TEST_DATABASE_URL", "")
live = pytest.mark.skipif(not DATABASE_URL, reason="SECURITY_CHECK_TEST_DATABASE_URL not set")


@pytest.fixture
def conn():
    import psycopg
    with psycopg.connect(DATABASE_URL) as c:
        yield c
        c.rollback()


def results_after(conn, seed="", min_pg_major=sc.DEFAULT_MIN_PG_MAJOR, auth=CLEAN_AUTH):
    """Every check, run in the transaction that applied ``seed`` (rolled back by the fixture)."""
    if seed:
        conn.execute(seed)
    conn.execute("SELECT set_config('teos329.api_schemas', 'public', true)")
    ctx = sc.Context(lambda sql, params=(): conn.execute(sql, params or None).fetchall(), auth, "",
                     "ci-drain", min_pg_major, ("public",))
    return {r.id: r for check in sc.CHECKS for r in check(ctx)}


@live
def test_ac1_the_database_from_the_ddl_passes_every_check(conn):
    results = results_after(conn)
    assert {i: r.objects for i, r in results.items() if r.status not in (sc.PASS, sc.WARN)} == {}
    assert sc.exit_code(list(results.values())) == sc.EXIT_OK
    assert sc.a1_line(lambda sql, p=(): conn.execute(sql).fetchall()).endswith("total=0")


#: AC2: one exposure each -> the check that must fail
EXPOSURES = {
    "anon USING(true) policy": ("B2", "CREATE POLICY open_read ON public.bundle FOR SELECT TO anon USING (true)"),
    "public WITH CHECK(true) policy": ("B2", "CREATE POLICY open_write ON public.app_config FOR INSERT WITH CHECK (true)"),
    "authenticated USING(true) policy": ("B2", "CREATE POLICY all_rows ON public.user_order FOR SELECT TO authenticated USING (true)"),
    "RLS off": ("B1", "ALTER TABLE public.bundle DISABLE ROW LEVEL SECURITY"),
    "RLS not forced": ("B1", "ALTER TABLE public.user_order NO FORCE ROW LEVEL SECURITY"),
    "anon write grant": ("B3", "GRANT INSERT, UPDATE, DELETE ON public.voucher TO anon"),
    "anon read grant": ("B3", "GRANT SELECT ON public.app_config TO anon"),
    "authenticated TRUNCATE": ("B4", "GRANT TRUNCATE ON public.user_order TO authenticated"),
    "anon default privileges": ("B6", "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO anon"),
    "anon-executable definer function": ("T4", "GRANT EXECUTE ON FUNCTION public.admin_end_session(uuid) TO anon"),
    "definer function executable through PUBLIC": (
        "T4", "GRANT EXECUTE ON FUNCTION public.admin_lookup_admins(text, integer, integer, uuid) TO PUBLIC"),
    "definer function without search_path": ("T5a", "ALTER FUNCTION public.sync_users_copy() RESET search_path"),
    "definer function with search_path public": (
        "T5a", "ALTER FUNCTION public.admin_end_session(uuid) SET search_path = public"),
    "extension in public": ("T6", "CREATE EXTENSION IF NOT EXISTS hstore WITH SCHEMA public"),
    "public storage bucket": ("T10a", "INSERT INTO storage.buckets (id, name, public) VALUES ('media', 'media', true)"),
    "anon policy on storage.objects": ("T10b", "CREATE POLICY anon_upload ON storage.objects FOR INSERT TO anon WITH CHECK (true)"),
    "no pgaudit": ("T11a", "DROP EXTENSION pgaudit"),
    "pgaudit logs parameters": ("T11a", "ALTER ROLE authenticator SET pgaudit.log_parameter = 'on'"),
}


@live
@pytest.mark.parametrize("exposure", EXPOSURES)
def test_ac2_each_exposure_fails_the_check(conn, exposure):
    check_id, seed = EXPOSURES[exposure]
    results = results_after(conn, seed)
    assert results[check_id].status == sc.FAIL, results[check_id]
    assert sc.exit_code(list(results.values())) == sc.EXIT_FAIL


@live
def test_ac2_anonymous_sign_in_fails(conn):
    results = results_after(conn, auth={**CLEAN_AUTH, "external": {"anonymous_users": True}})
    assert results["T8a"].status == sc.FAIL and sc.exit_code(list(results.values())) == sc.EXIT_FAIL


@live
def test_ac2_old_postgres_major_fails(conn):
    results = results_after(conn, min_pg_major=99)
    assert results["T12"].status == sc.FAIL and results["T12"].objects[0].startswith("PostgreSQL ")


@live
def test_ac2_the_a1_line_counts_the_exposure(conn):
    conn.execute("GRANT SELECT, INSERT ON public.bundle TO anon")
    line = sc.a1_line(lambda sql, p=(): conn.execute(sql).fetchall())
    assert "anon_read=1 " in line and "anon_write=1 " in line and line.endswith("total=2")


class RecordingConnection:
    """psycopg connection that records every statement the script sends."""

    def __init__(self, url):
        self.inner = sc.connect(url)
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return self.inner.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __enter__(self):
        self.inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self.inner.__exit__(*exc)


@live
def test_ac3_full_run_reads_only_and_prints_no_value():
    recorded = []

    def connect(url):
        c = RecordingConnection(url)
        recorded.append(c)
        return c

    out = io.StringIO()
    env = {"SECURITY_CHECK_DATABASE_URL": DATABASE_URL, "SUPABASE_URL": "https://ref.supabase.co",
           "SUPABASE_ANON_KEY": "anon-key-value-must-not-print"}
    code = sc.run(["--log-drain", "ci-drain"], environ=env, connect_fn=connect, get=fake_get(), out=out)
    text = out.getvalue()
    assert code == sc.EXIT_OK, text
    assert recorded[0].inner.read_only is True
    assert all(s.lstrip().upper().startswith(("SELECT", "WITH")) for s in recorded[0].statements)
    for value in ("anon-key-value-must-not-print", DATABASE_URL, "ref.supabase.co"):
        assert value not in text
    assert "SUPABASE_ANON_KEY=set" in text and "A.1 exposure:" in text and "(exit 0)" in text


@live
def test_without_auth_url_or_log_drain_the_run_is_incomplete_exit_3():
    out = io.StringIO()
    assert sc.run([], environ={"SECURITY_CHECK_DATABASE_URL": DATABASE_URL}, out=out) == sc.EXIT_UNKNOWN
    assert "INCOMPLETE" in out.getvalue()
