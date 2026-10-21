"""Proof that the database is frozen and nothing external is contacted.

The MCP adapter adds no table, column, migration, SQL script, RPC, function, trigger,
index, constraint, grant or RLS policy. It also has no repository of its own: every read
and write it performs goes through an existing repository against an existing table. These
tests are the mechanical proof of both, plus the proof that the MCP test suite itself never
reaches a real database or a real external service.
"""

import subprocess
from pathlib import Path

import pytest

from tests.mcp.conftest import executable_source

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: The MCP-only database objects this branch must not use, require or reference anywhere.
FORBIDDEN_DB_OBJECTS = (
    "mcp_purchase_idempotency",
    "mcp_card_checkout",
    "mcp_stripe_webhook_event",
)

#: The existing tables the MCP adapter is allowed to touch, through existing repositories.
ALLOWED_TABLES = {"user_order", "user_profile"}

MCP_SOURCE_FILES = [
    "app/config/feature_flags.py",
    "app/config/mcp_constants.py",
    "app/schemas/mcp.py",
    "app/schemas/mcp_card.py",
    "app/services/mcp_common.py",
    "app/services/mcp_purchase_service.py",
    "app/services/mcp_card_service.py",
    "app/api/v1/mcp_user_bundle.py",
]


# --- (23) No MCP database object is referenced ---------------------------------------


@pytest.mark.parametrize("forbidden", FORBIDDEN_DB_OBJECTS)
def test_no_mcp_only_database_object_is_referenced_anywhere(forbidden):
    """(23) Not in application source, not in SQL, not in tests, not in configuration."""
    # ``-w`` matches the whole identifier, so the unrelated error code
    # ``MCP_CARD_CHECKOUT_UNAVAILABLE`` -- which names no database object -- is not a hit.
    result = subprocess.run(
        ["git", "grep", "-l", "-i", "-w", forbidden, "--", "app", "tests", "locales", "docs",
         "supabase_ddl.sql", ".env.example", "Dockerfile", "requirements.txt"],
        cwd=PROJECT_ROOT, capture_output=True, text=True)
    # git grep exits 1 with no output when nothing matched, which is the passing case.
    assert result.stdout.strip() == "", f"{forbidden} is referenced in {result.stdout}"


def test_no_migration_directory_or_sql_script_was_added():
    """No forward SQL, no rollback SQL, no migration runner, no RPC definition."""
    tracked = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                             cwd=PROJECT_ROOT, capture_output=True, text=True).stdout
    touched = [line[3:] for line in tracked.splitlines() if line]
    offenders = [path for path in touched
                 if path.endswith(".sql") or path.startswith("migrations/")
                 or "migration" in path.lower()]
    assert offenders == [], offenders


def test_the_committed_schema_file_is_untouched():
    """``supabase_ddl.sql`` is the project's schema of record and must not move."""
    diff = subprocess.run(["git", "diff", "HEAD", "--", "supabase_ddl.sql"],
                          cwd=PROJECT_ROOT, capture_output=True, text=True)
    assert diff.stdout.strip() == ""


def test_no_mcp_repository_or_model_was_added():
    """The adapter owns no persistence layer of its own."""
    for directory in ("app/repo", "app/models"):
        offenders = [path.name for path in (PROJECT_ROOT / directory).glob("*mcp*")]
        assert offenders == [], offenders


def test_no_new_database_table_name_appears_in_the_mcp_source():
    """Every table the adapter touches already exists in ``DatabaseTables``."""
    from app.config.db import DatabaseTables

    known = {member.value for member in DatabaseTables}
    for relative in MCP_SOURCE_FILES:
        text = executable_source(PROJECT_ROOT / relative)
        # The adapter never names a table directly: it goes through a repository.
        for table in known:
            assert f'"{table}"' not in text
        assert "DatabaseTables" not in text
        assert ".table(" not in text
        assert "supabase_client" not in text
        assert "rpc(" not in text


def test_mcp_source_issues_no_sql_and_no_ddl():
    """(23) There is no statement anywhere in the adapter that could alter a schema."""
    for relative in MCP_SOURCE_FILES:
        text = executable_source(PROJECT_ROOT / relative).lower()
        for statement in ("create table", "alter table", "drop table", "create index",
                          "create function", "create trigger", "create policy", "grant ",
                          "insert into", "update set", "delete from", "execute("):
            assert statement not in text


def test_mcp_persistence_goes_only_through_existing_repositories():
    """The adapter uses ``UserOrderRepo``/``UserProfileRepo`` and nothing else."""
    for relative in ("app/services/mcp_purchase_service.py", "app/services/mcp_card_service.py"):
        text = executable_source(PROJECT_ROOT / relative)
        repos = {name for name in ("UserOrderRepo", "UserProfileRepo", "UserProfileBundleRepo",
                                   "UserWalletRepo", "BundleRepo", "ConfigRepo", "CurrencyRepo",
                                   "PromotionRepo", "VoucherRepo") if name in text}
        assert repos <= {"UserOrderRepo", "UserProfileRepo"}, repos


def test_the_card_adapter_writes_only_one_existing_column():
    """The single order write is ``payment_intent_code``, a column that already exists."""
    from app.models.user import UserOrderModel

    text = (PROJECT_ROOT / "app/services/mcp_card_service.py").read_text(encoding="utf-8")
    assert '"payment_intent_code"' in text
    assert "payment_intent_code" in UserOrderModel.model_fields


def test_the_wallet_adapter_writes_nothing_at_all():
    """It only reads back what the shared flow wrote."""
    text = executable_source(PROJECT_ROOT / "app/services/mcp_purchase_service.py")
    for writer in (".create(", ".update(", ".update_by(", ".delete(", ".upsert("):
        assert writer not in text


# --- (24) No test contacts a real database or external system ------------------------


def test_network_is_blocked_for_every_mcp_test():
    """The guard the whole package relies on actually refuses a connection."""
    import socket

    with pytest.raises(AssertionError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("127.0.0.1", 9))


def test_no_mcp_test_uses_a_real_client_or_key():
    """No test constructs a Supabase client, a Stripe key or an eSIM hub client."""
    for path in (PROJECT_ROOT / "tests" / "mcp").glob("*.py"):
        text = executable_source(path)
        for forbidden in ("create_client(", "supabase_client(", "stripe.api_key",
                          "sk_live", "sk_test", "whsec_", "httpx.Client", "requests."):
            assert forbidden not in text, f"{path.name} references {forbidden}"


def test_mcp_tests_never_import_a_repository_directly():
    """Every repository in these tests is a mock handed to an injectable constructor."""
    for path in (PROJECT_ROOT / "tests" / "mcp").glob("*.py"):
        text = executable_source(path)
        assert "from app.repo" not in text
        assert "import app.repo" not in text
