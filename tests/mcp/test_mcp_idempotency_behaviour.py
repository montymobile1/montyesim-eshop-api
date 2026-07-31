"""End-to-end idempotency behaviour of the MCP wallet purchase endpoint.

These tests drive the real router, service and repositories against the in-process
fake database, so every assertion is about observable behaviour: how many orders
exist, how often the wallet moved, and how often the eSIM Hub was asked to
provision. The invariant under test is always the same one: *one Idempotency-Key
buys at most one bundle.*
"""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config.mcp_constants import MCP_WALLET_BUNDLE_ASSIGN
from app.i18n import translate
from app.main import esim_app
from app.services.mcp_idempotency import build_request_hash, hash_idempotency_key
from tests.mcp.conftest import (
    BUNDLE_CODE,
    OTHER_VALID_KEY,
    USER_A_ID,
    USER_B_TOKEN,
    VALID_KEY,
    mcp_body,
    post_mcp,
)

IDEMPOTENCY_TABLE = "mcp_purchase_idempotency"
BUNDLE_PRICE = 10.0
STARTING_BALANCE = 100.0


# ------------------------------------------------------------------- helpers

def orders(db) -> list:
    return db.tables.get("user_order", [])


def transactions(db) -> list:
    return db.tables.get("user_wallet_transaction", [])


def records(db) -> list:
    return db.tables.get(IDEMPOTENCY_TABLE, [])


def balance(db, user_id: str = USER_A_ID) -> float:
    return next(row["amount"] for row in db.tables["user_wallet"] if row["user_id"] == user_id)


def set_balance(db, amount: float, user_id: str = USER_A_ID) -> None:
    for row in db.tables["user_wallet"]:
        if row["user_id"] == user_id:
            row["amount"] = amount


def data_of(response) -> dict:
    return response.json()["data"]


def seed_record(db, status: str = "PROCESSING", age_seconds: int = 0, order_id: str | None = None,
                key: str = VALID_KEY, user_id: str = USER_A_ID, body: dict | None = None,
                has_side_effects: bool = False, expires_in_seconds: int = 24 * 3600,
                response_code: int | None = None, response_body: dict | None = None) -> dict:
    """Insert an idempotency record the way a previous execution would have left it."""
    body = body or mcp_body()
    now = datetime.now(tz=timezone.utc)
    return db.seed(IDEMPOTENCY_TABLE, {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "operation": MCP_WALLET_BUNDLE_ASSIGN,
        "idempotency_key_hash": hash_idempotency_key(raw_key=key, user_id=user_id,
                                                     operation=MCP_WALLET_BUNDLE_ASSIGN),
        "request_hash": build_request_hash(user_id=user_id, operation=MCP_WALLET_BUNDLE_ASSIGN,
                                           bundle_code=body["bundle_code"],
                                           payment_type=body["payment_type"],
                                           related_search=body.get("related_search"),
                                           currency="USD"),
        "status": status,
        "order_id": order_id,
        "response_code": response_code,
        "response_body": response_body,
        "error_code": None,
        "has_side_effects": has_side_effects,
        "created_at": (now - timedelta(seconds=age_seconds)).isoformat(),
        "updated_at": (now - timedelta(seconds=age_seconds)).isoformat(),
        "expires_at": (now + timedelta(seconds=expires_in_seconds)).isoformat(),
    })


# ------------------------------------------------------------ successful replay

def test_replaying_the_same_key_never_buys_a_second_bundle(db, hub, client, mcp_enabled):
    first = post_mcp(client)
    second = post_mcp(client)

    assert (first.status_code, second.status_code) == (200, 200)
    assert data_of(first)["idempotent_replay"] is False
    assert data_of(second)["idempotent_replay"] is True
    assert data_of(second)["order_id"] == data_of(first)["order_id"]
    assert len(orders(db)) == 1
    assert len(transactions(db)) == 1
    assert balance(db) == STARTING_BALANCE - BUNDLE_PRICE
    assert hub.create_order_calls == 1


def test_replay_is_announced_in_the_response_header(db, hub, client, mcp_enabled):
    assert post_mcp(client).headers["X-Idempotent-Replay"] == "false"
    assert post_mcp(client).headers["X-Idempotent-Replay"] == "true"


def test_replay_is_stable_across_many_retries(db, hub, client, mcp_enabled):
    order_id = data_of(post_mcp(client))["order_id"]

    for _ in range(5):
        replay = post_mcp(client)
        assert replay.status_code == 200
        assert data_of(replay)["order_id"] == order_id

    assert len(orders(db)) == 1
    assert balance(db) == STARTING_BALANCE - BUNDLE_PRICE
    assert hub.create_order_calls == 1


def test_replay_carries_a_fresh_correlation_id(db, hub, client, mcp_enabled):
    first, second = post_mcp(client), post_mcp(client)
    assert data_of(second)["correlation_id"] != data_of(first)["correlation_id"]


def test_replay_does_not_re_read_the_bundle_or_the_hub(db, hub, client, mcp_enabled):
    post_mcp(client)
    calls_after_purchase = hub.get_bundle_calls
    post_mcp(client)
    assert hub.get_bundle_calls == calls_after_purchase


def test_a_new_key_starts_a_new_purchase(db, hub, client, mcp_enabled):
    first = post_mcp(client)
    second = post_mcp(client, idempotency_key=OTHER_VALID_KEY)

    assert (first.status_code, second.status_code) == (200, 200)
    assert data_of(second)["idempotent_replay"] is False
    assert data_of(second)["order_id"] != data_of(first)["order_id"]
    assert len(orders(db)) == 2
    assert balance(db) == STARTING_BALANCE - (2 * BUNDLE_PRICE)


def test_the_same_key_belongs_to_one_user_only(db, hub, client, mcp_enabled):
    mine = post_mcp(client)
    theirs = post_mcp(client, token=USER_B_TOKEN)

    assert (mine.status_code, theirs.status_code) == (200, 200)
    assert data_of(theirs)["idempotent_replay"] is False
    assert data_of(theirs)["order_id"] != data_of(mine)["order_id"]
    assert len(records(db)) == 2
    assert len({record["idempotency_key_hash"] for record in records(db)}) == 2


# ------------------------------------------------------- request identity rules

def test_the_same_key_with_a_different_purchase_is_a_conflict(db, hub, client, mcp_enabled):
    post_mcp(client)
    conflict = post_mcp(client, mcp_body(bundle_code="a-completely-different-bundle"))

    assert conflict.status_code == 409
    assert "already used for a different purchase" in conflict.json()["developerMessage"]
    assert conflict.json()["title"] == translate("IDEMPOTENCY_KEY_CONFLICT")
    assert len(orders(db)) == 1
    assert balance(db) == STARTING_BALANCE - BUNDLE_PRICE
    assert hub.create_order_calls == 1


def test_a_conflict_is_raised_while_the_first_purchase_is_still_running(db, hub, client, mcp_enabled):
    seed_record(db, status="PROCESSING", age_seconds=0)
    conflict = post_mcp(client, mcp_body(bundle_code="another-bundle"))

    assert conflict.status_code == 409
    assert "already used for a different purchase" in conflict.json()["developerMessage"]
    assert orders(db) == []


def test_a_logically_identical_request_replays_instead_of_conflicting(db, hub, client, mcp_enabled):
    ordered = mcp_body(related_search={"region": None,
                                       "countries": [{"iso3_code": "FRA", "country_name": "France"},
                                                     {"iso3_code": "ITA", "country_name": "Italy"}]})
    # Same purchase, expressed with a different country order, casing and padding.
    shuffled = mcp_body(related_search={"region": None,
                                        "countries": [{"iso3_code": " ita ", "country_name": "Italy "},
                                                      {"iso3_code": "fra", "country_name": " France"}]})

    first = post_mcp(client, ordered)
    second = post_mcp(client, shuffled)

    assert (first.status_code, second.status_code) == (200, 200)
    assert data_of(second)["idempotent_replay"] is True
    assert len(orders(db)) == 1


def test_quote_reference_does_not_change_the_request_identity(db, hub, client, mcp_enabled):
    first = post_mcp(client, mcp_body(quote_reference="quote-one"))
    second = post_mcp(client, mcp_body(quote_reference="quote-two"))

    assert second.status_code == 200
    assert data_of(second)["idempotent_replay"] is True
    # The stored outcome is replayed verbatim, so the original reference is echoed back.
    assert data_of(second)["quote_reference"] == "quote-one"
    assert data_of(first)["quote_reference"] == "quote-one"
    assert len(orders(db)) == 1


# --------------------------------------------------------------- in-flight keys

def test_a_request_still_in_flight_is_rejected_rather_than_duplicated(db, hub, client, mcp_enabled):
    seed_record(db, status="PROCESSING", age_seconds=0)
    response = post_mcp(client)

    assert response.status_code == 409
    assert response.json()["title"] == translate("IDEMPOTENT_REQUEST_IN_PROGRESS")
    # The caller is told to reuse the very same key, never to mint a new one.
    assert "Retry the very same Idempotency-Key" in response.json()["developerMessage"]
    assert orders(db) == []
    assert hub.create_order_calls == 0
    assert balance(db) == STARTING_BALANCE


def test_concurrent_requests_with_one_key_buy_exactly_one_bundle(db, hub, mcp_enabled):
    def purchase():
        return post_mcp(TestClient(esim_app, raise_server_exceptions=False))

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [future.result() for future in [pool.submit(purchase), pool.submit(purchase)]]

    statuses = sorted(response.status_code for response in responses)
    # The loser either finds the key in flight (409) or replays the winner's result (200).
    assert statuses in ([200, 200], [200, 409])
    assert len(orders(db)) == 1
    assert len(transactions(db)) == 1
    assert hub.create_order_calls == 1
    assert balance(db) == STARTING_BALANCE - BUNDLE_PRICE


# ------------------------------------------------------------ crash reconciliation

def test_a_crashed_execution_that_completed_is_recovered_as_success(db, hub, client, mcp_enabled):
    order_id = data_of(post_mcp(client))["order_id"]
    # Simulate a process that died after provisioning but before writing the outcome.
    records(db)[0].update({
        "status": "PROCESSING", "response_code": None, "response_body": None,
        "updated_at": (datetime.now(tz=timezone.utc) - timedelta(seconds=600)).isoformat()})

    recovered = post_mcp(client)

    assert recovered.status_code == 200
    assert data_of(recovered)["status"] == "COMPLETED"
    assert data_of(recovered)["idempotent_replay"] is True
    assert data_of(recovered)["order_id"] == order_id
    assert data_of(recovered)["message"] == "Recovered from an interrupted execution"
    assert len(orders(db)) == 1
    assert hub.create_order_calls == 1
    assert records(db)[0]["status"] == "SUCCEEDED"


def test_a_crashed_execution_of_unknown_outcome_is_escalated_not_retried(db, hub, client, mcp_enabled):
    seed_record(db, status="PROCESSING", age_seconds=600)
    response = post_mcp(client)

    assert response.status_code == 424
    assert data_of(response)["status"] == "MANUAL_INTERVENTION_REQUIRED"
    assert data_of(response)["next_action"] == "CONTACT_SUPPORT"
    assert data_of(response)["idempotent_replay"] is True
    assert orders(db) == []
    assert hub.create_order_calls == 0
    assert balance(db) == STARTING_BALANCE
    assert records(db)[0]["status"] == "AMBIGUOUS"


# ------------------------------------------------------------- failure replays

def test_an_insufficient_balance_fails_finally_and_replays_the_failure(db, hub, client, mcp_enabled):
    set_balance(db, 1.0)

    first = post_mcp(client)
    assert first.status_code == 400
    assert data_of(first)["status"] == "FAILED"
    assert data_of(first)["payment_status"] == "NOT_CHARGED"
    assert data_of(first)["next_action"] == "RETRY_WITH_NEW_IDEMPOTENCY_KEY"
    assert orders(db) == []
    assert transactions(db) == []

    second = post_mcp(client)
    assert second.status_code == 400
    assert data_of(second)["idempotent_replay"] is True
    assert second.headers["X-Idempotent-Replay"] == "true"
    assert records(db)[0]["status"] == "FAILED_FINAL"
    # Topping up does not resurrect a finally-failed key: a new key is required.
    set_balance(db, STARTING_BALANCE)
    assert post_mcp(client).status_code == 400
    assert post_mcp(client, idempotency_key=OTHER_VALID_KEY).status_code == 200


def test_an_unavailable_hub_fails_retryably_and_the_same_key_may_be_retried(db, hub, client, mcp_enabled):
    hub.raise_on_get_bundle = True

    first = post_mcp(client)
    assert first.status_code == 503
    assert data_of(first)["payment_status"] == "NOT_CHARGED"
    assert data_of(first)["next_action"] == "RETRY_SAME_IDEMPOTENCY_KEY"
    assert records(db)[0]["status"] == "FAILED_RETRYABLE"
    assert orders(db) == []

    hub.raise_on_get_bundle = False
    retry = post_mcp(client)
    assert retry.status_code == 200
    assert data_of(retry)["idempotent_replay"] is False
    assert len(orders(db)) == 1
    assert balance(db) == STARTING_BALANCE - BUNDLE_PRICE


def test_an_unknown_bundle_does_not_consume_the_wallet(db, hub, client, mcp_enabled):
    response = post_mcp(client, mcp_body(bundle_code="does-not-exist"))

    assert response.status_code == 400
    assert data_of(response)["order_status"] == "NOT_CREATED"
    assert balance(db) == STARTING_BALANCE
    assert transactions(db) == []
    assert records(db)[0]["status"] == "FAILED_FINAL"


# -------------------------------------------- charged but not provisioned (424)

def test_provisioning_failure_after_the_debit_is_escalated_never_reported_as_success(db, hub, client,
                                                                                     mcp_enabled):
    hub.fail_provisioning = True
    response = post_mcp(client)

    assert response.status_code == 424
    assert data_of(response)["status"] == "MANUAL_INTERVENTION_REQUIRED"
    assert data_of(response)["payment_status"] == "UNKNOWN_OR_SUCCESS"
    assert data_of(response)["order_status"] == "FAILURE_OR_UNKNOWN"
    assert data_of(response)["provisioning_status"] == "UNKNOWN"
    assert data_of(response)["next_action"] == "CONTACT_SUPPORT"
    assert balance(db) == STARTING_BALANCE - BUNDLE_PRICE
    assert records(db)[0]["status"] == "AMBIGUOUS"


def test_an_escalated_purchase_is_never_re_executed_by_a_retry(db, hub, client, mcp_enabled):
    hub.fail_provisioning = True
    post_mcp(client)

    hub.fail_provisioning = False  # the hub recovers, the key must still not re-charge
    replay = post_mcp(client)

    assert replay.status_code == 424
    assert data_of(replay)["idempotent_replay"] is True
    assert data_of(replay)["next_action"] == "CONTACT_SUPPORT"
    assert balance(db) == STARTING_BALANCE - BUNDLE_PRICE
    assert len(transactions(db)) == 1
    assert hub.create_order_calls == 1


def test_a_hub_exception_after_the_debit_is_escalated_not_reported_as_not_charged(db, hub, client,
                                                                                  mcp_enabled):
    hub.raise_on_provisioning = True
    response = post_mcp(client)

    assert response.status_code == 424
    assert data_of(response)["payment_status"] == "UNKNOWN_OR_SUCCESS"
    assert balance(db) == STARTING_BALANCE - BUNDLE_PRICE
    assert records(db)[0]["status"] == "AMBIGUOUS"


# ---------------------------------------- terminal keys are never recycled (TTL)

def expire(db, index: int = 0) -> None:
    """Push a record's retention hint into the past. It must change nothing."""
    records(db)[index]["expires_at"] = (datetime.now(tz=timezone.utc) - timedelta(days=30)).isoformat()


def test_an_expired_successful_key_still_replays_and_never_buys_again(db, hub, client, mcp_enabled):
    first = post_mcp(client)
    expire(db)

    second = post_mcp(client)

    assert second.status_code == 200
    assert data_of(second)["idempotent_replay"] is True
    assert data_of(second)["order_id"] == data_of(first)["order_id"]
    assert len(orders(db)) == 1
    assert len(transactions(db)) == 1
    assert balance(db) == STARTING_BALANCE - BUNDLE_PRICE
    assert hub.create_order_calls == 1


def test_an_expired_ambiguous_key_stays_blocked_forever(db, hub, client, mcp_enabled):
    hub.fail_provisioning = True
    post_mcp(client)
    assert records(db)[0]["status"] == "AMBIGUOUS"
    expire(db)

    hub.fail_provisioning = False
    replay = post_mcp(client)

    assert replay.status_code == 424
    assert data_of(replay)["next_action"] == "CONTACT_SUPPORT"
    assert balance(db) == STARTING_BALANCE - BUNDLE_PRICE
    assert len(transactions(db)) == 1
    assert hub.create_order_calls == 1


def test_an_expired_finally_failed_key_stays_terminal(db, hub, client, mcp_enabled):
    set_balance(db, 1.0)
    assert post_mcp(client).status_code == 400
    assert records(db)[0]["status"] == "FAILED_FINAL"
    expire(db)

    set_balance(db, STARTING_BALANCE)
    replay = post_mcp(client)

    # Elapsed time must not turn a spent key back into a fresh one.
    assert replay.status_code == 400
    assert data_of(replay)["idempotent_replay"] is True
    assert orders(db) == []
    # A new key is the only way forward.
    assert post_mcp(client, idempotency_key=OTHER_VALID_KEY).status_code == 200


def test_an_expired_retryable_key_with_no_side_effect_may_still_be_retried(db, hub, client, mcp_enabled):
    hub.raise_on_get_bundle = True
    assert post_mcp(client).status_code == 503
    record = records(db)[0]
    assert record["status"] == "FAILED_RETRYABLE"
    assert record["order_id"] is None
    assert record["has_side_effects"] is False
    expire(db)

    hub.raise_on_get_bundle = False
    retry = post_mcp(client)

    assert retry.status_code == 200
    assert data_of(retry)["idempotent_replay"] is False
    assert len(orders(db)) == 1


def test_a_retryable_key_that_touched_something_is_not_re_executed(db, hub, client, mcp_enabled):
    # A FAILED_RETRYABLE record that nonetheless carries a side effect must replay,
    # never re-execute: this is the guard against double-charging on retry.
    seed_record(db, status="FAILED_RETRYABLE", has_side_effects=True, response_code=503,
                response_body={"status": "FAILED", "payment_status": "NOT_CHARGED",
                               "order_status": "NOT_CREATED", "provisioning_status": "NOT_STARTED",
                               "next_action": "RETRY_SAME_IDEMPOTENCY_KEY", "idempotent_replay": False,
                               "correlation_id": None})

    response = post_mcp(client)

    assert response.status_code == 503
    assert data_of(response)["idempotent_replay"] is True
    assert orders(db) == []
    assert hub.create_order_calls == 0
    assert balance(db) == STARTING_BALANCE


def test_a_retryable_key_that_created_an_order_is_not_re_executed(db, hub, client, mcp_enabled):
    seed_record(db, status="FAILED_RETRYABLE", order_id=str(uuid.uuid4()), response_code=503,
                response_body={"status": "FAILED", "payment_status": "NOT_CHARGED",
                               "order_status": "FAILURE", "provisioning_status": "NOT_STARTED",
                               "next_action": "RETRY_SAME_IDEMPOTENCY_KEY", "idempotent_replay": False,
                               "correlation_id": None})

    response = post_mcp(client)

    assert response.status_code == 503
    assert data_of(response)["idempotent_replay"] is True
    assert hub.create_order_calls == 0


def test_a_successful_record_is_marked_as_having_side_effects(db, hub, client, mcp_enabled):
    post_mcp(client)
    record = records(db)[0]
    assert record["has_side_effects"] is True
    assert record["order_id"] is not None


def test_a_live_key_is_replayed_rather_than_recycled(db, hub, client, mcp_enabled):
    post_mcp(client)
    assert data_of(post_mcp(client))["idempotent_replay"] is True
    assert len(orders(db)) == 1


# ----------------------------------------------------------------- persistence

def test_the_raw_idempotency_key_is_never_persisted(db, hub, client, mcp_enabled):
    post_mcp(client)
    record = records(db)[0]

    assert VALID_KEY not in json.dumps(record, default=str)
    assert len(record["idempotency_key_hash"]) == 64
    assert len(record["request_hash"]) == 64


def test_the_stored_record_links_the_order_and_the_outcome(db, hub, client, mcp_enabled):
    response = post_mcp(client)
    record = records(db)[0]

    assert record["status"] == "SUCCEEDED"
    assert record["response_code"] == 200
    assert record["order_id"] == data_of(response)["order_id"]
    assert record["error_code"] is None
    assert record["operation"] == MCP_WALLET_BUNDLE_ASSIGN
    assert record["user_id"] == USER_A_ID


def test_the_stored_body_holds_no_per_request_values(db, hub, client, mcp_enabled):
    post_mcp(client)
    stored = records(db)[0]["response_body"]

    # Correlation id and the replay flag are re-stamped on every replay, never served stale.
    assert stored["correlation_id"] is None
    assert stored["idempotent_replay"] is False
    assert stored["status"] == "COMPLETED"


def test_one_record_per_key_even_after_repeated_calls(db, hub, client, mcp_enabled):
    for _ in range(4):
        post_mcp(client)
    assert len(records(db)) == 1
    assert orders(db)[0]["bundle_id"] == BUNDLE_CODE
