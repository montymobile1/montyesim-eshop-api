"""TEOS-271: row level security seen from a client, against a live Supabase stack.

Two users sign in (GoTrue), a row of every per-user table is seeded for each with the service key,
then each user reads every table through PostgREST with their own JWT, exactly as a modified client
or a hand-crafted supabase-js query would.

The test creates users and rows and removes them afterwards, so point it at a disposable stack or
at QA only, never at production. It is skipped unless these are set:

    RLS_TEST_SUPABASE_URL   https://<project>.supabase.co  (or RLS_TEST_REST_URL + RLS_TEST_AUTH_URL)
    RLS_TEST_ANON_KEY       the anon key
    RLS_TEST_SERVICE_KEY    the service_role key
    RLS_TEST_DATABASE_URL   optional: postgres URL for the pg_tables / pg_policies check
"""
import os
import uuid

import httpx
import pytest

from tests.rls.test_rls_ddl_assertion import ADMIN_READ, NO_POLICY, OWNED, expected_policies

BASE = os.getenv("RLS_TEST_SUPABASE_URL", "").rstrip("/")
REST = os.getenv("RLS_TEST_REST_URL") or (f"{BASE}/rest/v1" if BASE else "")
AUTH = os.getenv("RLS_TEST_AUTH_URL") or (f"{BASE}/auth/v1" if BASE else "")
ANON_KEY = os.getenv("RLS_TEST_ANON_KEY", "")
SERVICE_KEY = os.getenv("RLS_TEST_SERVICE_KEY", "")
DATABASE_URL = os.getenv("RLS_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not (REST and AUTH and ANON_KEY and SERVICE_KEY),
                                reason="RLS_TEST_* not set: no live Supabase stack to test against")

PASSWORD = "Rls-Test-Passw0rd!"


def _headers(key, token=None):
    return {"apikey": key, "Authorization": f"Bearer {token or key}", "Content-Type": "application/json",
            "Prefer": "return=representation"}


SERVICE = _headers(SERVICE_KEY)


def _ok(response):
    assert response.status_code < 300, f"{response.request.method} {response.request.url}: " \
                                       f"{response.status_code} {response.text}"
    return response.json() if response.content else None


class Stack:
    def __init__(self):
        self.http = httpx.Client(timeout=30)
        self.users, self.rows = [], []  # rows: (table, key column, value) to delete afterwards

    def user(self, anonymous=False, **metadata):
        """A new user and their access token: an email user (with ``app_metadata`` / ``user_metadata``
        if given), or an anonymous sign-in like the app's."""
        if anonymous:
            session = _ok(self.http.post(f"{AUTH}/signup", headers={"apikey": ANON_KEY}, json={}))
        else:
            email = f"rls-{uuid.uuid4().hex[:12]}@example.com"
            _ok(self.http.post(f"{AUTH}/admin/users", headers=SERVICE,
                               json={"email": email, "password": PASSWORD, "email_confirm": True, **metadata}))
            session = _ok(self.http.post(f"{AUTH}/token?grant_type=password", headers={"apikey": ANON_KEY},
                                         json={"email": email, "password": PASSWORD}))
        user_id = session["user"]["id"]
        self.users.append(user_id)
        return user_id, session["access_token"]

    def insert(self, table, row, key="id"):
        created = _ok(self.http.post(f"{REST}/{table}", headers=SERVICE, json=row))[0]
        self.rows.append((table, key, created[key]))
        return created

    def read(self, table, token=None, **filters):
        """GET /rest/v1/<table> as a user (token) or as anon (no token): status and rows."""
        params = {"select": "*", **{k: f"eq.{v}" for k, v in filters.items()}}
        response = self.http.get(f"{REST}/{table}", params=params, headers=_headers(ANON_KEY, token))
        return response.status_code, response.json()

    def cleanup(self):
        for table, key, value in reversed(self.rows):
            self.http.delete(f"{REST}/{table}", params={key: f"eq.{value}"}, headers=SERVICE)
        for table in ("users_copy",):  # written by the auth.users trigger
            for user_id in self.users:
                self.http.delete(f"{REST}/{table}", params={"id": f"eq.{user_id}"}, headers=SERVICE)
        for user_id in self.users:
            self.http.delete(f"{AUTH}/admin/users/{user_id}", headers=SERVICE)
        self.http.close()


def seed(stack, user_id):
    """One row of every per-user table owned by ``user_id``; table -> primary key value."""
    order = stack.insert("user_order", {"user_id": user_id, "amount": 1, "currency": "USD"})
    profile = stack.insert("user_profile", {"user_id": user_id, "user_order_id": order["id"],
                                            "iccid": f"89{uuid.uuid4().int % 10**17:017d}",
                                            "smdp_address": "smdp.example", "activation_code": "LPA:1$x$y"})
    bundle = stack.insert("user_profile_bundle", {"user_id": user_id, "user_order_id": order["id"],
                                                  "user_profile_id": profile["id"], "bundle_data": {}})
    wallet = stack.insert("user_wallet", {"user_id": user_id, "currency": "USD"})
    transaction = stack.insert("user_wallet_transaction", {"wallet_id": wallet["id"], "amount": 5,
                                                           "status": "success", "source": "rls-test"})
    usage = stack.insert("promotion_usage", {"user_id": user_id})
    device = stack.insert("device", {"user_id": user_id, "device_id": f"rls-{uuid.uuid4().hex}",
                                     "is_logged_in": True})
    notification = stack.insert("notification", {"user_id": user_id})
    return {"user_order": order["id"], "user_profile": profile["id"], "user_profile_bundle": bundle["id"],
            "user_wallet": wallet["id"], "user_wallet_transaction": transaction["id"],
            "promotion_usage": usage["id"], "device": device["id"], "notification": notification["id"]}


@pytest.fixture(scope="module")
def world():
    stack = Stack()
    try:
        a_id, a_token = stack.user()
        b_id, b_token = stack.user()
        empty_id, empty_token = stack.user()
        anon_id, anon_token = stack.user(anonymous=True)
        _, admin_token = stack.user(app_metadata={"role": "admin"})
        _, self_made_admin_token = stack.user(user_metadata={"role": "admin"})  # users can set this
        rows = {"a": seed(stack, a_id), "b": seed(stack, b_id)}
        # an order made in an anonymous session: owned through anonymous_user_id
        rows["anon_order"] = stack.insert("user_order", {"user_id": a_id, "anonymous_user_id": anon_id,
                                                         "amount": 1, "currency": "USD"})["id"]
        stack.insert("user_otp", {"email": "rls-otp@example.com", "otp": "hash"})
        rows["contact_us"] = stack.insert("contact_us", {"email": "rls-contact@example.com",
                                                         "content": "rls test"})["id"]
        rows["users_copy"] = {"a": a_id, "b": b_id}  # written by the auth.users trigger
        yield {"stack": stack, "rows": rows, "tokens": {"a": a_token, "b": b_token},
               "empty": empty_token, "anonymous": anon_token, "admin": admin_token,
               "self_made_admin": self_made_admin_token}
    finally:
        stack.cleanup()


@pytest.mark.parametrize("table", OWNED)
@pytest.mark.parametrize("who,other", [("a", "b"), ("b", "a")])
def test_each_user_sees_exactly_their_own_row(world, table, who, other):
    stack, rows = world["stack"], world["rows"]
    status, seen = stack.read(table, world["tokens"][who])

    assert status == 200
    ids = [row["id"] for row in seen]
    expected = [rows[who][table]]
    if table == "user_order" and who == "a":
        expected.append(rows["anon_order"])  # user_id is a's
    assert sorted(map(str, ids)) == sorted(map(str, expected))
    assert str(rows[other][table]) not in map(str, ids)


@pytest.mark.parametrize("table", OWNED)
def test_the_server_key_still_reads_every_users_rows(world, table):
    """The API's key (service_role) bypasses RLS: it reads both users' rows, as every route does."""
    stack, rows = world["stack"], world["rows"]
    ids = ",".join(str(rows[who][table]) for who in ("a", "b"))
    response = stack.http.get(f"{REST}/{table}", params={"select": "id", "id": f"in.({ids})"}, headers=SERVICE)

    assert sorted(str(row["id"]) for row in _ok(response)) == sorted(str(rows[w][table]) for w in ("a", "b"))


@pytest.mark.parametrize("table", OWNED)
def test_another_users_row_by_primary_key_is_an_empty_set_not_403(world, table):
    stack, rows = world["stack"], world["rows"]

    assert stack.read(table, world["tokens"]["a"], id=rows["b"][table]) == (200, [])


@pytest.mark.parametrize("table", [*OWNED, *NO_POLICY])
def test_anon_key_without_jwt_sees_no_rows(world, table):
    assert world["stack"].read(table) == (200, [])


@pytest.mark.parametrize("table", OWNED)
def test_user_without_rows_gets_an_empty_result(world, table):
    assert world["stack"].read(table, world["empty"]) == (200, [])


@pytest.mark.parametrize("table", NO_POLICY)
@pytest.mark.parametrize("who", ["a", "b"])
def test_tables_without_policy_return_nothing_to_authenticated(world, table, who):
    assert world["stack"].read(table, world["tokens"][who]) == (200, [])


def test_anonymous_session_sees_its_order_through_anonymous_user_id(world):
    status, seen = world["stack"].read("user_order", world["anonymous"])

    assert status == 200
    assert [row["id"] for row in seen] == [world["rows"]["anon_order"]]


@pytest.mark.parametrize("table", OWNED)
def test_client_writes_are_refused(world, table):
    """No INSERT / UPDATE / DELETE policy: a user cannot change even their own rows directly."""
    stack, rows, token = world["stack"], world["rows"], world["tokens"]["a"]
    headers = _headers(ANON_KEY, token)
    params = {"id": f"eq.{rows['a'][table]}"}

    assert stack.http.delete(f"{REST}/{table}", params=params, headers=headers).json() == []
    assert stack.http.patch(f"{REST}/{table}", params=params, headers=headers, json={}).status_code in (200, 400)
    assert stack.read(table, token, id=rows["a"][table])[1] != []  # still there


def test_invoker_rpc_cannot_read_other_users_rows(world):
    """get_wallet_transactions_with_count(NULL) used to return every wallet's transactions."""
    response = world["stack"].http.post(f"{REST}/rpc/get_wallet_transactions_with_count",
                                        headers=_headers(ANON_KEY, world["tokens"]["a"]),
                                        json={"p_user_id": None, "p_page_size": 100, "p_page_index": 0})
    if response.status_code in (401, 403, 404):
        return  # revoked from authenticated: nothing to read either
    ids = {str(row.get("id")) for row in (response.json().get("data") or [])}
    assert str(world["rows"]["b"]["user_wallet_transaction"]) not in ids


# ---------- portal admins (app_metadata.role) ----------

def _seeded(world, table):
    rows = world["rows"]
    if table == "contact_us":
        return {str(rows["contact_us"])}
    if table == "users_copy":
        return {str(rows["users_copy"]["a"]), str(rows["users_copy"]["b"])}
    return {str(rows["a"][table]), str(rows["b"][table])}


@pytest.mark.parametrize("table", ADMIN_READ)
def test_portal_admin_reads_every_users_rows_of_the_portal_tables(world, table):
    status, seen = world["stack"].read(table, world["admin"])

    assert status == 200
    assert _seeded(world, table) <= {str(row["id"]) for row in seen}


@pytest.mark.parametrize("table", [t for t in (*OWNED, *NO_POLICY) if t not in ADMIN_READ])
def test_portal_admin_reads_nothing_else(world, table):
    assert world["stack"].read(table, world["admin"]) == (200, [])


@pytest.mark.parametrize("table", ADMIN_READ)
def test_a_role_in_user_metadata_is_not_an_admin(world, table):
    status, seen = world["stack"].read(table, world["self_made_admin"])

    assert status == 200
    assert not _seeded(world, table) & {str(row["id"]) for row in seen}


@pytest.mark.skipif(not DATABASE_URL, reason="RLS_TEST_DATABASE_URL not set")
def test_catalog_shows_rls_and_the_select_policies():
    import psycopg
    with psycopg.connect(DATABASE_URL) as conn:
        tables = dict(conn.execute(
            "select tablename, rowsecurity from pg_tables where schemaname = 'public' and tablename = any(%s)",
            [list(OWNED) + list(NO_POLICY)]).fetchall())
        forced = dict(conn.execute(
            "select relname, relforcerowsecurity from pg_class where relnamespace = 'public'::regnamespace "
            "and relname = any(%s)", [list(OWNED) + list(NO_POLICY)]).fetchall())
        policies = conn.execute(
            "select tablename, policyname, cmd, roles from pg_policies where schemaname = 'public' "
            "and tablename = any(%s)", [list(OWNED) + list(NO_POLICY)]).fetchall()

    assert all(tables[t] and forced[t] for t in (*OWNED, *NO_POLICY))
    assert sorted((t, p, c, list(r)) for t, p, c, r in policies) == \
           sorted((t, name, "SELECT", ["authenticated"]) for t in (*OWNED, *NO_POLICY)
                  for name in expected_policies(t))
