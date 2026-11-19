"""How an eSIM Hub order / top-up call failed, so a paid order is only refunded when that is safe.

`EsimHubService.create_reseller_order` and `create_reseller_topup` return `None` for every kind of
failure. Some of those failures mean the Hub never created anything; others mean it may have (or
did). Refunding the second kind would give away an eSIM that exists, so the caller passes a
`HubCallOutcome` and the Hub service records which kind of failure it was. It is written on failure
paths only: a successful call never touches it.

    DEFINITE   HTTP 200 with "success": false. The Hub answered and refused the order.
               The only case that is refunded automatically.
    AMBIGUOUS  everything else: HTTP 4xx / 5xx, timeout, connection error, unreadable or
               unexpected response, and any failure after the Hub accepted the order (activation
               code lookup, response parsing). Never refunded automatically: manual review.

A failure nobody classified (for example a call that raised before the Hub was reached) stays
unclassified and is treated as AMBIGUOUS.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Optional


class HubFailureKind(StrEnum):
    DEFINITE = "definite"
    AMBIGUOUS = "ambiguous"


@dataclass
class HubCallOutcome:
    kind: Optional[HubFailureKind] = None
    detail: str = ""

    def mark(self, kind: HubFailureKind, detail: str) -> None:
        """Record the failure. The first classification wins: a later, more generic error of the same
        call (e.g. the exception that follows a refused response) does not overwrite it."""
        if self.kind is None:
            self.kind = kind
            self.detail = detail

    @property
    def definite(self) -> bool:
        return self.kind == HubFailureKind.DEFINITE

    @property
    def reason(self) -> str:
        if self.kind is None:
            return "eSIM Hub failure was not classified; treated as ambiguous"
        return f"{self.kind}: {self.detail}"
