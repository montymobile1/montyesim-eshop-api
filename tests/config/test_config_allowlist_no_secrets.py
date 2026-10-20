"""Story: stop leaking server secrets via /api/v1/app/configurations (cases 1-25; case 16 is
test_secret_source_guard.py).

app_config and every other table live in an in-memory stand-in for the Supabase client (BaseRepository's
client); the Hub is reached through a mocked httpx. No network, no database.
"""

import asyncio
import os
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from loguru import logger

from app.config import helper
from app.config.helper import MissingSecretError, get_secret, get_setting
from app.services import app_service as app_service_module
from scripts import remove_secrets_from_app_config as script

SECRETS = {
    "ESIM_HUB_API_KEY": "secret-hub-key-from-db",
    "DCB_HUB_API_KEY": "secret-dcb-key-from-db",
    "RESELLER_ID": "secret-reseller-guid",
    "SMTP_USERNAME": "secret-smtp-user",
    "SMTP_PASSWORD": "secret-smtp-password",
    "DCB_USERNAME": "secret-dcb-user",
    "STRIPE_SECRET_KEY": "secret-stripe",
    "SOME_TOKEN": "secret-token",
}
PRIVATE_SETTINGS = {
    "WALLET_TOP_UP_ALERT_RECIPIENTS": "ops-a@example.com,ops-b@example.com",
    "SUPPORT_EMAIL": "support-staff@example.com",
    "SMTP_SENDER": "noreply-staff@example.com",
    "SMTP_SENDER_NAME": "Esim Support",
    "BASE_URL": "https://portal.example.com",
    "APP_CACHE_KEY": "cache-v42",
    "CACHE_KEY": "bundles-v7",
    "FORBIDDEN_COUNTRIES": "XX",
    "DEFAULT_DCB_PROVIDER": "NONE",
}
CLIENT_SETTINGS = {
    "ALLOWED_PAYMENT_TYPES": "Wallet,Card",
    "login_type": "email_phone_both",  # stored lowercase (ConfigKeysEnum.LOGIN_TYPE), returned uppercased as today
    "OTP_EXPIRATION_TIME": "5",
    "TRANSACTION_EXPIRY_TIME": "10",
    "REFERRAL_CODE_PERCENTAGE": "20",
    "REFERRAL_CODE_AMOUNT": "5",
    "CASHBACK_DISCOUNT": "3",
    "COMPANY_SUPPORT_PHONE": "+96170000000",
    "COMPANY_SUPPORT_EMAIL": "help@example.com",
    "TOP_COUNTRIES_COUNT": "9",
    "DEFAULT_CURRENCY": "USD",
    "WHATSAPP_NUMBER": "+96171111111",
}
# Keys each client reads from the response (client inventory, docs/config-allowlist.md).
CLIENT_INVENTORY = {
    "ALLOWED_PAYMENT_TYPES": "mobile, web, iOS SDK, Android SDK",
    "LOGIN_TYPE": "mobile, web, iOS SDK, Android SDK",
    "OTP_EXPIRATION_TIME": "web",
    "TRANSACTION_EXPIRY_TIME": "web",
    "REFERRAL_CODE_PERCENTAGE": "mobile",
    "REFERRAL_CODE_AMOUNT": "web, iOS SDK, mobile (old builds)",
    "CASHBACK_DISCOUNT": "Android SDK",
    "COMPANY_SUPPORT_PHONE": "web",
    "COMPANY_SUPPORT_EMAIL": "web",
    "TOP_COUNTRIES_COUNT": "web",
    "WHATSAPP_NUMBER": "mobile, web, iOS SDK, Android SDK",
    "CATALOG.BUNDLES_CACHE_VERSION": "mobile, web, iOS SDK, Android SDK",
    "SUPABASE_BASE_URL": "mobile, iOS SDK, Android SDK",
    "SUPABASE_BASE_ANON_KEY": "mobile, iOS SDK, Android SDK",
    "default_currency": "mobile, web, iOS SDK, Android SDK (exact lowercase)",
}
COMPUTED = ["CATALOG.BUNDLES_CACHE_VERSION", "WHATSAPP_NUMBER", "SUPABASE_BASE_URL", "SUPABASE_BASE_ANON_KEY",
            "default_currency"]
ENV = {
    "WHATSAPP_NUMBER": "+96172222222",
    "SUPABASE_URL": "https://project.supabase.co",
    "SUPABASE_ANON_KEY": "public-anon-key",
    "DEFAULT_CURRENCY": "EUR",
    "ESIM_HUB_API_KEY": "secret-hub-key-from-env",
    "DCB_HUB_API_KEY": "secret-dcb-key-from-env",
    "RESELLER_ID": "secret-reseller-guid",
}


# --------------------------------------------------------------------------- in-memory Supabase

class FakeQuery:
    def __init__(self, db, table, action="select", data=None, ignore_duplicates=False):
        self.db, self.table, self.action, self.data = db, table, action, data
        self.ignore_duplicates = ignore_duplicates
        self.preds = []

    def select(self, *_):
        return self

    def eq(self, key, value):
        self.preds.append(lambda r: r.get(key) == getattr(value, "value", value))
        return self

    def filter(self, key, _op, value):
        return self.eq(key, value)

    def limit(self, *_):
        return self

    def offset(self, *_):
        return self

    def order(self, *_, **__):
        return self

    def execute(self):
        if self.db.down:
            raise ConnectionError("could not connect to server")
        with self.db.lock:
            rows = self.db.tables.setdefault(self.table, [])
            if self.action == "select":
                return MagicMock(data=[dict(r) for r in rows if all(p(r) for p in self.preds)])
            if self.action == "delete":
                kept = [r for r in rows if not all(p(r) for p in self.preds)]
                removed = len(rows) - len(kept)
                rows[:] = kept
                return MagicMock(data=[{}] * removed)
            row = {**self.data, "id": len(rows) + 1}
            if any(r["key"] == row["key"] for r in rows):  # app_config.key is unique
                if self.ignore_duplicates:
                    return MagicMock(data=[])
                raise RuntimeError('duplicate key value violates unique constraint "app_config_key_key"')
            rows.append(row)
            self.db.inserts.append(row["key"])
            return MagicMock(data=[dict(row)])


class FakeTable:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def select(self, *_):
        return FakeQuery(self.db, self.name)

    def insert(self, data):
        return FakeQuery(self.db, self.name, "insert", data)

    def upsert(self, data, on_conflict=None, ignore_duplicates=False):
        assert on_conflict == "key"
        return FakeQuery(self.db, self.name, "insert", data, ignore_duplicates)

    def delete(self):
        return FakeQuery(self.db, self.name, "delete")


class FakeDb:
    def __init__(self, app_config=()):
        self.tables = {"app_config": [{"id": i + 1, "key": k, "value": v} for i, (k, v) in enumerate(app_config)]}
        self.down = False
        self.inserts = []
        self.lock = threading.Lock()

    def table(self, name):
        return FakeTable(self, getattr(name, "value", name))

    def config(self):
        return {r["key"]: r["value"] for r in self.tables["app_config"]}


def interleaved_rows():
    """app_config as it can be today: secrets and private settings mixed in with what clients read."""
    secrets, private, client = list(SECRETS.items()), list(PRIVATE_SETTINGS.items()), list(CLIENT_SETTINGS.items())
    rows = []
    while secrets or private or client:
        for source in (client, secrets, private):
            if source:
                rows.append(source.pop(0))
    return rows


@pytest.fixture
def env(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    return ENV


@pytest.fixture
def db(env):
    database = FakeDb(interleaved_rows())
    with patch("app.repo.base_repo.supabase_client", return_value=database):
        yield database


@pytest.fixture
def service(db):
    from app.services.app_service import AppService
    return AppService()


def configurations(service):
    return asyncio.run(service.configurations())


def as_pairs(response):
    return [(c.key, c.value) for c in response.data]


def keys_of(response):
    return [c.key for c in response.data]


# --------------------------------------------------------------------------- HTTP harness

@pytest.fixture(scope="module")
def esim_app():
    """The real FastAPI app, imported without config lookups (services read settings when constructed)."""
    import app.config.config as config_module

    def _stub(key, default_value=None):
        return default_value if default_value is not None else "stub"

    with patch.object(helper, "get_config", _stub), patch.object(config_module, "get_config", _stub):
        from app.main import esim_app as application
        return application


@pytest.fixture
def http(esim_app, service):
    with patch("app.api.v1.application.service", service):
        yield TestClient(esim_app, raise_server_exceptions=False)


def get_configurations(http, headers=None):
    return http.get("/api/v1/app/configurations", headers={"x-device-id": "device-1"} if headers is None else headers)


@pytest.fixture
def logs():
    lines = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    yield lines
    logger.remove(sink)


# --------------------------------------------------------------------------- what clients get

def test_01_every_allowlisted_key_present_in_app_config_is_returned(service):
    returned = as_pairs(configurations(service))
    for key, value in CLIENT_SETTINGS.items():
        assert (key.upper(), value) in returned


def test_02_computed_entries_still_present(service, env):
    returned = as_pairs(configurations(service))
    assert ("CATALOG.BUNDLES_CACHE_VERSION", "cache-v42") in returned
    assert ("SUPABASE_BASE_URL", env["SUPABASE_URL"]) in returned
    assert ("SUPABASE_BASE_ANON_KEY", env["SUPABASE_ANON_KEY"]) in returned
    assert ("default_currency", env["DEFAULT_CURRENCY"]) in returned
    assert ("WHATSAPP_NUMBER", env["WHATSAPP_NUMBER"]) in returned


def test_10_esim_hub_api_key_absent_even_if_the_row_exists(service, db):
    assert db.config()["ESIM_HUB_API_KEY"]
    assert "ESIM_HUB_API_KEY" not in keys_of(configurations(service))


def test_11_wallet_top_up_alert_recipients_absent(service):
    assert "WALLET_TOP_UP_ALERT_RECIPIENTS" not in keys_of(configurations(service))


def test_12_reseller_id_and_smtp_sender_absent(service):
    keys = keys_of(configurations(service))
    assert "RESELLER_ID" not in keys
    assert "SMTP_SENDER" not in keys


def test_13_unknown_new_key_is_private_by_default(service, db):
    db.tables["app_config"].append({"id": 999, "key": "SOME_NEW_SETTING", "value": "x"})
    assert "SOME_NEW_SETTING" not in keys_of(configurations(service))


@pytest.mark.parametrize("key", ["FOO_KEY", "FOO_SECRET", "FOO_PASSWORD", "FOO_TOKEN", "FOO_USERNAME",
                                 "ESIM_HUB_API_KEY", "RESELLER_ID", "smtp_password"])
def test_14_secret_shaped_key_absent_whatever_the_allowlist_says(service, db, key):
    db.tables["app_config"].append({"id": 999, "key": key, "value": "leak-me"})
    with patch.object(app_service_module, "CLIENT_CONFIG_KEYS",
                      app_service_module.CLIENT_CONFIG_KEYS | {key.upper(), "SUPABASE_BASE_ANON_KEY"}):
        response = configurations(service)
    assert key.upper() not in keys_of(response)
    assert all(value != "leak-me" for _, value in as_pairs(response))
    assert "SUPABASE_BASE_ANON_KEY" in keys_of(response)  # the approved exception


def test_14b_response_holds_no_secret_value_at_all(service):
    values = {value for _, value in as_pairs(configurations(service))}
    assert values.isdisjoint(SECRETS.values())
    assert values.isdisjoint(set(PRIVATE_SETTINGS.values()) - {"cache-v42"})  # APP_CACHE_KEY's value is the
    # catalog version clients read as CATALOG.BUNDLES_CACHE_VERSION, as today


def test_21_allowlisted_null_is_returned_as_null(service, monkeypatch):
    """app_config.value is NOT NULL, so a null can only come from a computed entry."""
    monkeypatch.delenv("SUPABASE_URL")
    assert ("SUPABASE_BASE_URL", None) in as_pairs(configurations(service))


def test_22_snapshot_key_order_and_shape_unchanged_for_allowlisted_keys(http, db):
    body = get_configurations(http).json()

    # today's order: app_config rows in the order the database returns them, then the computed entries
    expected_db_part = [r["key"].upper() for r in db.tables["app_config"]
                        if r["key"].upper() in app_service_module.CLIENT_CONFIG_KEYS]
    assert [item["key"] for item in body["data"]] == expected_db_part + COMPUTED
    assert expected_db_part == ["ALLOWED_PAYMENT_TYPES", "LOGIN_TYPE", "OTP_EXPIRATION_TIME", "TRANSACTION_EXPIRY_TIME",
                                "REFERRAL_CODE_PERCENTAGE", "REFERRAL_CODE_AMOUNT", "CASHBACK_DISCOUNT",
                                "COMPANY_SUPPORT_PHONE", "COMPANY_SUPPORT_EMAIL", "TOP_COUNTRIES_COUNT",
                                "DEFAULT_CURRENCY", "WHATSAPP_NUMBER"]
    assert all(set(item) == {"key", "value"} and isinstance(item["value"], str) for item in body["data"])
    assert set(body) == {"status", "totalCount", "data", "title", "message", "developerMessage", "responseCode"}
    assert (body["status"], body["responseCode"], body["totalCount"]) == ("success", 200, len(body["data"]))


def test_23_changed_allowlisted_value_is_visible_on_the_next_call(service, db):
    assert dict(as_pairs(configurations(service)))["LOGIN_TYPE"] == "email_phone_both"
    for row in db.tables["app_config"]:
        if row["key"] == "login_type":
            row["value"] = "phone"
    assert dict(as_pairs(configurations(service)))["LOGIN_TYPE"] == "phone"


def test_25_response_contains_every_key_from_the_client_inventory(http):
    body = get_configurations(http).json()
    returned = [item["key"] for item in body["data"]]
    for key in CLIENT_INVENTORY:
        if key == "default_currency":
            assert key in returned  # some builds match it exactly, lowercase
        else:
            assert key.lower() in [k.lower() for k in returned], key
    for key in COMPUTED:
        assert key in returned  # the oldest mobile builds match these exactly and crash without them


# --------------------------------------------------------------------------- endpoint errors and logs

def test_17_no_device_id_is_400_as_today(http):
    response = get_configurations(http, headers={})
    assert response.status_code == 400


def test_18_logs_of_the_response_contain_no_secret_value(http, logs):
    assert get_configurations(http).status_code == 200
    text = "\n".join(logs)
    for value in SECRETS.values():
        assert value not in text


def test_18b_hub_request_log_masks_the_credentials(db, logs, env):
    from app.config.config import esim_hub_service_instance
    hub = esim_hub_service_instance()
    reply = httpx.Response(200, json={"success": True, "data": {"items": []}},
                           request=httpx.Request("GET", "https://hub.example.com"))
    with patch("httpx.AsyncClient.request", AsyncMock(return_value=reply)):
        asyncio.run(hub.get_exchange_rates(["USD"]))
    text = "\n".join(logs)
    assert "Request: curl" in text
    assert env["ESIM_HUB_API_KEY"] not in text
    assert '--header "Api-Key: ******"' in text


def test_20_db_down_is_5xx_without_env_fallback(http, db, monkeypatch):
    monkeypatch.setenv("LOGIN_TYPE", "phone")
    db.down = True
    response = get_configurations(http)
    assert response.status_code == 503
    body = response.json()
    assert body["data"] is None
    assert "phone" not in response.text and "could not connect" not in response.text


# --------------------------------------------------------------------------- get_secret / get_setting

def test_03_get_secret_returns_the_env_value(env):
    assert get_secret("ESIM_HUB_API_KEY") == env["ESIM_HUB_API_KEY"]


def test_04_get_secret_never_writes_to_app_config(db):
    before = db.config()
    get_secret("ESIM_HUB_API_KEY")
    get_secret("RESELLER_ID")
    assert db.inserts == []
    assert db.config() == before


def test_05_get_setting_still_auto_seeds_non_secret_keys_from_env(db, monkeypatch):
    monkeypatch.setenv("BRAND_NEW_SETTING", "from-env")
    assert get_setting("BRAND_NEW_SETTING") == "from-env"
    assert db.config()["BRAND_NEW_SETTING"] == "from-env"
    assert get_setting("UNSET_SETTING", "fallback") == "fallback"
    assert db.config()["UNSET_SETTING"] == "fallback"
    assert get_setting("SUPPORT_EMAIL") == PRIVATE_SETTINGS["SUPPORT_EMAIL"]  # existing rows win, as today


@pytest.mark.parametrize("key", ["ESIM_HUB_API_KEY", "DCB_HUB_API_KEY", "RESELLER_ID", "SMTP_USERNAME",
                                 "SMTP_PASSWORD", "DCB_USERNAME", "STRIPE_SECRET_KEY", "ANY_TOKEN", "reseller_id"])
def test_15_get_setting_raises_on_a_secret_shaped_key(db, key):
    with pytest.raises(ValueError):
        get_setting(key)
    assert db.inserts == []


@pytest.mark.parametrize("key", ["APP_CACHE_KEY", "CACHE_KEY", "WALLET_TOP_UP_ALERT_RECIPIENTS", "SMTP_SENDER"])
def test_15b_approved_non_secrets_still_read_through_get_setting(db, key):
    assert get_setting(key) == PRIVATE_SETTINGS[key]


def test_19_missing_env_secret_is_a_loud_error_not_an_empty_string_to_the_hub(db, monkeypatch):
    from app.config.config import esim_hub_service_instance
    monkeypatch.delenv("ESIM_HUB_API_KEY")
    with pytest.raises(MissingSecretError, match="ESIM_HUB_API_KEY is not set"):
        esim_hub_service_instance()  # even though the old app_config row still holds a value

    monkeypatch.setenv("ESIM_HUB_API_KEY", "set")
    hub = esim_hub_service_instance()
    monkeypatch.setenv("RESELLER_ID", "")
    with patch("httpx.AsyncClient.request", AsyncMock()) as request:
        with pytest.raises(MissingSecretError, match="RESELLER_ID"):
            asyncio.run(hub.get_exchange_rates(["USD"]))
    request.assert_not_called()


def test_24_concurrent_first_get_setting_seeds_one_row(db, monkeypatch):
    from app.repo.config_repo import ConfigRepo
    monkeypatch.setenv("RACE_SETTING", "v")
    barrier = threading.Barrier(2)
    real_get_first_by = ConfigRepo.get_first_by
    results, errors = [], []

    def both_miss(self, *args, **kwargs):
        found = real_get_first_by(self, *args, **kwargs)
        barrier.wait(timeout=5)  # both readers see no row before either inserts
        return found

    def read():
        try:
            results.append(get_setting("RACE_SETTING"))
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)

    with patch.object(ConfigRepo, "get_first_by", both_miss):
        threads = [threading.Thread(target=read) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert errors == []
    assert results == ["v", "v"]
    assert [r["key"] for r in db.tables["app_config"]].count("RACE_SETTING") == 1


# --------------------------------------------------------------------------- flows that read secrets

def test_06_hub_calls_still_authenticate_after_the_switch(db, env):
    from app.config.config import esim_hub_service_instance
    from app.services.integration.hub_dcb_service import HubDcbService
    reply = httpx.Response(200, json={"success": True, "data": {"items": []}},
                           request=httpx.Request("GET", "https://hub.example.com"))
    with patch("httpx.AsyncClient.request", AsyncMock(return_value=reply)) as request:
        asyncio.run(esim_hub_service_instance().get_exchange_rates(["USD"]))

    sent = request.await_args.kwargs
    assert sent["headers"]["Api-Key"] == env["ESIM_HUB_API_KEY"]  # the environment, not the stale row
    assert sent["params"]["ResellerGuid"] == env["RESELLER_ID"]
    assert HubDcbService().get_api_key() == env["DCB_HUB_API_KEY"]


def test_06b_callbacks_still_match_the_reseller(env):
    from app.services import callback_service
    source = open(callback_service.__file__).read()
    assert source.count('get_secret("RESELLER_ID")') == 2
    assert get_secret("RESELLER_ID") == env["RESELLER_ID"]


def test_07_wallet_top_up_and_purchase_alert_emails_reach_configured_recipients(db):
    from app.config.db import PaymentTypeEnum
    from app.services.bundle_service import BundleService
    from app.services.user_wallet_service import UserWalletService

    template = MagicMock()
    template.render.return_value = "<html/>"
    wallet_service = UserWalletService()
    wallet_service._UserWalletService__user_wallet_transaction_repo = MagicMock(list_since=MagicMock(return_value=[]))
    wallet_service._UserWalletService__user_wallet_repo = MagicMock(list_in=MagicMock(return_value=[]))
    wallet = MagicMock(id="wid", amount=40.0, currency="USD")
    tx = MagicMock(id="tx-1", amount=25.0, created_at="2026-10-20T10:15:00+00:00")
    # the real DB-backed reader (importing app.main with stubbed settings can leave a stub bound in the module)
    with patch("app.services.user_wallet_service.get_config", get_setting), \
            patch("app.services.user_wallet_service.get_email_template", return_value=template), \
            patch("app.services.user_wallet_service.send_email") as send:
        wallet_service._UserWalletService__send_top_up_admin_email(
            MagicMock(email="u@example.com", metadata={}), wallet, tx, "pi_1")
    assert send.call_args.kwargs["recipients"] == PRIVATE_SETTINGS["WALLET_TOP_UP_ALERT_RECIPIENTS"]

    bundle_service = BundleService.__new__(BundleService)
    bundle_service._BundleService__currency_service = MagicMock(get_currency_rate=MagicMock(return_value=1.0))
    bundle_service._BundleService__user_wallet_repo = MagicMock(get_first_by=MagicMock(return_value=wallet))
    order = MagicMock(id="order-1", currency="USD", amount=1000, modified_amount=0, tax_amount=0,
                      payment_intent_code="pi_2")
    with patch.dict(os.environ, {"SEND_ESIM_PURCHASE_NOTIFICATION": "true"}), \
            patch("app.services.bundle_service.get_config", get_setting), \
            patch("app.services.bundle_service.get_email_template", return_value=template), \
            patch("app.services.bundle_service.send_email") as send:
        bundle_service._BundleService__send_purchase_admin_email(
            user=MagicMock(email="u@example.com", metadata={}), user_order=order,
            bundle=MagicMock(countries=[], bundle_name="B"), payment_type=PaymentTypeEnum.WALLET)
    assert send.call_args.kwargs["recipients"] == PRIVATE_SETTINGS["WALLET_TOP_UP_ALERT_RECIPIENTS"]


# --------------------------------------------------------------------------- clean-up script

def script_db():
    return FakeDb(interleaved_rows())


def test_08_script_removes_secret_rows_and_reports_names_not_values(env):
    database, out = script_db(), []
    environ = {**env, "SMTP_USERNAME": "different-value"}  # others missing
    result = script.remove_secret_rows(database.table("app_config"), list(database.tables["app_config"]),
                                       apply=True, environ=environ, log=out.append)

    assert sorted(result.removed) == sorted(SECRETS)
    assert set(database.config()) == set(PRIVATE_SETTINGS) | set(CLIENT_SETTINGS)  # APP_CACHE_KEY, CACHE_KEY kept
    text = "\n".join(out)
    for value in list(SECRETS.values()) + list(environ.values()):
        assert value not in text
    assert "ESIM_HUB_API_KEY" in text
    # the env check: ESIM/DCB hub keys differ from the stale rows, SMTP_USERNAME differs, the rest are missing
    assert "!!! WARNING: ESIM_HUB_API_KEY: ENV DIFFERS" in text
    assert "!!! WARNING: SMTP_PASSWORD: ENV MISSING" in text
    assert "RESELLER_ID: " not in text.split("!!! WARNING")[0] and "removed RESELLER_ID (env matches)" in text


def test_08b_script_dry_run_changes_nothing(env):
    database = script_db()
    before = database.config()
    result = script.remove_secret_rows(database.table("app_config"), list(database.tables["app_config"]),
                                       apply=False, environ=env, log=lambda _: None)
    assert sorted(result.removed) == sorted(SECRETS)
    assert database.config() == before


def test_09_script_rerun_is_safe(env):
    database = script_db()
    for _ in range(2):
        rows = list(database.tables["app_config"])
        result = script.remove_secret_rows(database.table("app_config"), rows, apply=True, environ=env,
                                           log=lambda _: None)
    assert result.removed == [] and result.failed == []
    assert set(database.config()) == set(PRIVATE_SETTINGS) | set(CLIENT_SETTINGS)


def test_script_main_prints_counts_and_names_only(env, monkeypatch, capsys):
    database = script_db()
    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_KEY", "service-role")
    with patch("supabase.create_client", return_value=database), patch("dotenv.load_dotenv"):
        assert script.main([]) == 0
        assert len(database.config()) == len(interleaved_rows())
        assert script.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert f"would_removed={len(SECRETS)} kept={len(PRIVATE_SETTINGS) + len(CLIENT_SETTINGS)}" in out
    assert f"removed={len(SECRETS)}" in out
    assert all(value not in out for value in SECRETS.values())
    assert "service-role" not in out


def test_script_uses_the_same_secret_definition_as_get_secret():
    assert script.is_secret_key is helper.is_secret_key
