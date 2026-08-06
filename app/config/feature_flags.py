"""Narrowly scoped MCP feature flags.

Two properties matter and are both deliberate:

* **They fail closed.** An unset, blank or unparseable value means *off*. A deployment
  that has never heard of MCP therefore serves no MCP purchase traffic.
* **They are read nowhere else.** Neither flag appears in the legacy request path, in
  ``callback_service`` or in any shared service, so turning MCP off (or on) has zero
  effect on the website, the mobile application or the Stripe webhook. Grepping for
  either name is the proof.

Values are read at call time via :func:`os.getenv` -- the project's existing
configuration style -- rather than captured at import, so the flag can be flipped
without a redeploy and a test can toggle it with ``monkeypatch.setenv``.
"""

import os

from app.config.mcp_constants import MCP_CARD_PURCHASE_ENABLED_ENV, MCP_PURCHASE_ENABLED_ENV

#: The only spellings accepted as "on". Anything else, including a typo, is off.
_TRUTHY = ("true", "1", "yes", "on")


def _flag(env_name: str) -> bool:
    return (os.getenv(env_name) or "").strip().lower() in _TRUTHY


def mcp_purchase_enabled() -> bool:
    """Master switch for every ``/api/v1/mcp/...`` purchase endpoint."""
    return _flag(MCP_PURCHASE_ENABLED_ENV)


def mcp_card_purchase_enabled() -> bool:
    """Switch for the card checkout endpoints specifically.

    Requires the master switch too: card checkout is a subset of MCP purchasing, so it
    can be disabled on its own but never enabled on its own.
    """
    return mcp_purchase_enabled() and _flag(MCP_CARD_PURCHASE_ENABLED_ENV)
