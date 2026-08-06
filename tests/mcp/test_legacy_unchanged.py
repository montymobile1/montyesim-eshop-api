"""Proof that the existing application is untouched.

The MCP adapter is additive by construction, and these tests are the mechanical proof of
it: the legacy purchase service, the webhook and the legacy routers are byte-identical to
their committed state; the OpenAPI document gained three paths and lost or changed none;
and neither MCP flag is read anywhere outside the MCP modules, so turning MCP off cannot
reach the website, the mobile application or Stripe.
"""

import subprocess
from pathlib import Path

import pytest

from tests.mcp.conftest import executable_source

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Files that carry the legacy purchase, payment, order, provisioning and auth behaviour.
#: Not one of them may differ from its committed state on this branch.
UNTOUCHED_FILES = [
    "app/services/user_service.py",
    "app/services/callback_service.py",
    "app/services/bundle_service.py",
    "app/services/user_wallet_service.py",
    "app/api/v1/user_bundle.py",
    "app/api/v1/user_wallet.py",
    "app/api/v1/callback.py",
    "app/api/v1/authentication.py",
    "app/dependencies/security.py",
    "app/schemas/bundle.py",
    "app/config/constants.py",
    "app/config/db.py",
    "app/models/user.py",
    "app/repo/user_order_repo.py",
    "app/repo/base_repo.py",
    "app/main.py",
    "supabase_ddl.sql",
]

#: Every MCP-only module. Nothing outside this list may mention an MCP flag.
MCP_MODULES = {
    "app/config/feature_flags.py",
    "app/config/mcp_constants.py",
    "app/schemas/mcp.py",
    "app/schemas/mcp_card.py",
    "app/services/mcp_common.py",
    "app/services/mcp_purchase_service.py",
    "app/services/mcp_card_service.py",
    "app/api/v1/mcp_user_bundle.py",
}


def git_diff(*paths) -> str:
    result = subprocess.run(["git", "diff", "HEAD", "--", *paths],
                            cwd=PROJECT_ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


# --- (1)(2)(3) Legacy Wallet, Card and webhook implementations are unmodified ---------


@pytest.mark.parametrize("relative_path", UNTOUCHED_FILES)
def test_legacy_source_file_is_unmodified(relative_path):
    """(1)(2)(3) Not one byte of the legacy purchase, payment or auth path changed."""
    assert git_diff(relative_path).strip() == "", f"{relative_path} was modified"


def test_legacy_tests_are_unmodified():
    """The existing test suite is unchanged, so its results are comparable."""
    assert git_diff("tests/services", "tests/conftest.py", "tests/mocks.py").strip() == ""


def test_only_expected_files_were_modified():
    """The whole tracked diff is the additive set, and nothing else."""
    result = subprocess.run(["git", "diff", "--name-only", "HEAD"],
                            cwd=PROJECT_ROOT, capture_output=True, text=True)
    modified = {line for line in result.stdout.splitlines() if line}
    allowed = {
        # One additive Stripe helper, alongside the untouched create_payment_intent.
        "app/config/utils.py",
        # One additive router registration.
        "app/api/v1/__init__.py",
        # Additive translation keys and documented environment variables.
        "locales/en.json",
        "locales/ar.json",
        ".env.example",
    }
    assert modified <= allowed, f"unexpected modifications: {sorted(modified - allowed)}"


def test_stripe_helper_change_is_purely_additive():
    """``create_payment_intent`` and every other existing helper is untouched."""
    diff = git_diff("app/config/utils.py")
    removed = [line for line in diff.splitlines()
               if line.startswith("-") and not line.startswith("---")]
    assert removed == [], removed


def test_router_registration_change_is_purely_additive():
    diff = git_diff("app/api/v1/__init__.py")
    removed = [line for line in diff.splitlines()
               if line.startswith("-") and not line.startswith("---")]
    assert removed == [], removed


@pytest.mark.parametrize("locale_file", ["locales/en.json", "locales/ar.json"])
def test_locale_change_adds_keys_without_changing_any(locale_file):
    """No existing translation is altered; only MCP keys are appended."""
    import json

    committed = subprocess.run(["git", "show", f"HEAD:{locale_file}"],
                               cwd=PROJECT_ROOT, capture_output=True, text=True)
    before = json.loads(committed.stdout)
    with open(PROJECT_ROOT / locale_file, encoding="utf-8") as handle:
        after = json.load(handle)

    assert all(after[key] == value for key, value in before.items())
    assert all(key.startswith("MCP_") for key in set(after) - set(before))


# --- (4) Legacy OpenAPI paths and schemas remain unchanged ---------------------------


def test_legacy_openapi_paths_are_unchanged(openapi_spec, legacy_openapi_baseline):
    """(4) Every pre-existing path is byte-identical, and none was removed."""
    live = openapi_spec["paths"]
    for path, definition in legacy_openapi_baseline["paths"].items():
        assert path in live, f"legacy path {path} disappeared"
        assert live[path] == definition, f"legacy path {path} changed shape"


def test_legacy_openapi_schemas_are_unchanged(openapi_spec, legacy_openapi_baseline):
    """(4) Every pre-existing component schema is byte-identical."""
    live = openapi_spec["components"]["schemas"]
    for name, definition in legacy_openapi_baseline["schemas"].items():
        assert name in live, f"legacy schema {name} disappeared"
        assert live[name] == definition, f"legacy schema {name} changed shape"


def test_openapi_gained_only_the_three_mcp_paths(openapi_spec, legacy_openapi_baseline):
    added = set(openapi_spec["paths"]) - set(legacy_openapi_baseline["paths"])
    assert added == {
        "/api/v1/mcp/user/bundle/assign",
        "/api/v1/mcp/user/bundle/card/checkout",
        "/api/v1/mcp/user/bundle/card/status/{payment_reference}",
    }


def test_openapi_gained_only_mcp_schemas(openapi_spec, legacy_openapi_baseline):
    added = set(openapi_spec["components"]["schemas"]) - set(legacy_openapi_baseline["schemas"])
    assert all("Mcp" in name for name in added), sorted(added)


# --- (5) The MCP flag reaches only MCP routes ----------------------------------------


def test_mcp_flags_are_read_only_inside_mcp_modules():
    """(5) Grepping is the proof: no legacy module can branch on an MCP flag."""
    offenders = []
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative in MCP_MODULES:
            continue
        text = path.read_text(encoding="utf-8")
        if "MCP_PURCHASE_ENABLED" in text or "MCP_CARD_PURCHASE_ENABLED" in text \
                or "mcp_purchase_enabled" in text or "mcp_card_purchase_enabled" in text:
            offenders.append(relative)
    assert offenders == []


def test_no_legacy_module_imports_an_mcp_module():
    """The dependency arrow points one way: MCP -> legacy, never the reverse."""
    offenders = []
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative in MCP_MODULES or relative == "app/api/v1/__init__.py":
            continue
        text = executable_source(path)
        for mcp_import in ("app.schemas.mcp", "app.services.mcp_", "app.config.mcp_constants",
                           "app.config.feature_flags", "McpPurchaseService", "McpCardCheckoutService"):
            if mcp_import in text:
                offenders.append((relative, mcp_import))
    assert offenders == []


def test_legacy_assign_route_still_accepts_the_legacy_request_shape(openapi_spec):
    """The website's own purchase route is untouched, down to its request body."""
    assign = openapi_spec["paths"]["/api/v1/user/bundle/assign"]["post"]
    body_ref = assign["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert body_ref.endswith("/AssignRequest")
    # And it still supports the guest-checkout path the MCP route deliberately does not.
    source = (PROJECT_ROOT / "app/api/v1/user_bundle.py").read_text(encoding="utf-8")
    assert "bearer_token_anonymous" in source


def test_mcp_routes_require_the_non_anonymous_bearer_dependency():
    """(6)(7) MCP uses the strict dependency, never the guest-tolerant one."""
    source = executable_source(PROJECT_ROOT / "app/api/v1/mcp_user_bundle.py")
    assert "bearer_token_anonymous" not in source
    assert source.count("bearer_token") >= 6  # two per route: gate and injected user
