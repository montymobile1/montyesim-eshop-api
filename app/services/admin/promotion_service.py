"""Promotions pages of the admin portal: list (MS-5050), add and edit (MS-5052), promo analysis (MS-5054).

- list: ``admin_search_promotions`` does the search (literal, name or code), the status tabs, the rule
  and bundle filters and the facets, with bound parameters only (no PostgREST filter strings built from
  user input, the injection of MS-5050);
- create: a strict body, the server sets ``type``, ``is_active`` and ``times_used``; uniqueness by the
  database (409 ``duplicate_code`` / ``duplicate_name``) with no pre-check; the bundle codes are checked
  by the ``validate_bundle_codes`` trigger (422);
- edit: ``code``, ``rule_id``, ``amount`` and ``valid_from`` are immutable once used (409
  ``promotion_used``), guarded in the same transaction with ``p_guard = {times_used: 0}``;
- status and delete: audited; a used promotion cannot be deleted (409 ``promotion_used`` with
  ``times_used``), enforced by the FK from ``promotion_usage.promotion_code`` as well;
- usage: ``admin_promotion_usage`` joins the promotion, the user and the bundle, and computes the
  totals over every matching row.
"""
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.exceptions.admin import AdminConflict, AdminNotFound, AdminValidationError
from app.repo.admin.base import AuditContext
from app.repo.admin.promotion_repo import PromotionAdminRepo
from app.repo.admin.promotion_rule_repo import PromotionRuleAdminRepo
from app.repo.admin.promotion_usage_repo import PromotionUsageAdminRepo
from app.schemas.admin.common import AdminStatusPatch, PageParams
from app.schemas.admin.promotions import (MAX_USAGE_DAYS, AdminPromotionCreate, AdminPromotionPage,
                                          AdminPromotionUpdate, AdminPromotionUsagePage)

PROMOTION_TYPE = "Promo"  # what the portal hard-coded before the API
IMMUTABLE_ONCE_USED = ("code", "rule_id", "amount", "valid_from")
MAX_PERCENTAGE = 100


# ---------------------------------------------------------------- shaping helpers

def _ref(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict) and value.get("id") is not None:
        return {"id": value["id"], "name": value.get("name")}
    return None


def shape_rule(rule: Any) -> Optional[Dict[str, Any]]:
    """``{id, name, max_usage, beneficiary, action, event}`` from a PostgREST embed or the RPC row."""
    if not isinstance(rule, dict) or rule.get("id") is None:
        return None
    return {
        "id": str(rule["id"]),
        "name": rule.get("name"),
        "max_usage": rule.get("max_usage"),
        "beneficiary": rule.get("beneficiary"),
        "action": _ref(rule.get("promotion_rule_action") or rule.get("action")),
        "event": _ref(rule.get("promotion_rule_event") or rule.get("event")),
    }


def split_bundle_codes(value: Any) -> List[str]:
    """The stored CSV as a list; blanks dropped."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def join_bundle_codes(codes: List[str]) -> Optional[str]:
    """The CSV the ``promotion.bundle_code`` column stores; None when the promotion applies to every bundle."""
    return ",".join(codes) if codes else None


def promotion_status(row: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """The tab a promotion belongs to (same rule as ``admin_search_promotions``)."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    valid_to = _naive(row.get("valid_to"))
    valid_from = _naive(row.get("valid_from"))
    if valid_to is not None and valid_to < now:
        return "expired"
    if valid_from is not None and valid_from > now:
        return "scheduled"
    return "active" if row.get("is_active", True) else "inactive"


def _naive(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def to_db_timestamp(value: Optional[datetime]) -> Optional[str]:
    """A datetime as the UTC text the ``timestamp`` (without time zone) columns store."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat()


def shape_promotion(row: Dict[str, Any]) -> Dict[str, Any]:
    """The API row from a PostgREST read (``promotion_rule`` embed) or an ``admin_search_promotions`` row."""
    rule = shape_rule(row.get("promotion_rule") or row.get("rule"))
    bundle_codes = row["bundle_codes"] if isinstance(row.get("bundle_codes"), list) \
        else split_bundle_codes(row.get("bundle_code"))
    return {
        "id": str(row.get("id")),
        "name": row.get("name"),
        "code": row.get("code"),
        "type": row.get("type"),
        "amount": row.get("amount"),
        "valid_from": row.get("valid_from"),
        "valid_to": row.get("valid_to"),
        "is_active": bool(row.get("is_active", True)),
        "times_used": int(row.get("times_used") or 0),
        "bundle_codes": bundle_codes,
        "rule_id": str(row["rule_id"]) if row.get("rule_id") else (rule["id"] if rule else None),
        "rule": rule,
        "status": row.get("status") or promotion_status(row),
        "created_at": row.get("created_at"),
    }


def is_amount_action(rule: Optional[Dict[str, Any]]) -> Optional[bool]:
    """True for DISCOUNT_AMOUNT / CASHBACK_AMOUNT, False for *_PERCENTAGE, None when unknown."""
    action = (rule or {}).get("promotion_rule_action") or (rule or {}).get("action") or {}
    name = str(action.get("name") or "").upper()
    if "AMOUNT" in name:
        return True
    if "PERCENT" in name:
        return False
    return None


def day_range(date_from: Optional[date], date_to: Optional[date]) -> Tuple[Optional[datetime], Optional[datetime]]:
    """``from`` / ``to`` days (inclusive, UTC) as ``[start, end)``; 422 when from > to or over 366 days."""
    if date_from and date_to:
        if date_from > date_to:
            raise AdminValidationError("from: must be on or before to")
        if (date_to - date_from).days + 1 > MAX_USAGE_DAYS:
            raise AdminValidationError(f"from/to: the range is at most {MAX_USAGE_DAYS} days")
    start = datetime.combine(date_from, time.min) if date_from else None
    end = datetime.combine(date_to + timedelta(days=1), time.min) if date_to else None
    return start, end


def shape_usage_row(row: Dict[str, Any]) -> Dict[str, Any]:
    promotion = row.get("promotion") if isinstance(row.get("promotion"), dict) else None
    user = row.get("user") if isinstance(row.get("user"), dict) else None
    return {
        "id": str(row.get("id")),
        "created_at": row.get("created_at"),
        "promotion_code": row.get("promotion_code"),
        "promotion": {"id": str(promotion["id"]), "code": promotion.get("code"), "name": promotion.get("name")}
        if promotion and promotion.get("id") else None,
        "user": {"id": str(user["id"]), "email": user.get("email"), "msisdn": user.get("msisdn")}
        if user and user.get("id") else None,
        "bundle_id": str(row["bundle_id"]) if row.get("bundle_id") else None,
        "bundle_code": row.get("bundle_code"),
        "order_id": str(row["order_id"]) if row.get("order_id") else None,
        "discount_amount": row.get("discount_amount"),
        "currency": row.get("currency"),
        "status": row.get("status"),
    }


def shape_totals(totals: Dict[str, Any]) -> Dict[str, Any]:
    amounts = totals.get("discount_amount") or []
    return {
        "uses": int(totals.get("uses") or 0),
        "distinct_users": int(totals.get("distinct_users") or 0),
        "discount_amount": [{"currency": item.get("currency"), "amount": float(item.get("amount") or 0)}
                            for item in amounts if isinstance(item, dict)],
    }


# ---------------------------------------------------------------- service

class PromotionAdminService:
    def __init__(self, repo: Optional[PromotionAdminRepo] = None, rules: Optional[PromotionRuleAdminRepo] = None,
                 usage: Optional[PromotionUsageAdminRepo] = None):
        self._repo = repo
        self._rules = rules
        self._usage = usage

    # created lazily so importing the router never opens a database client
    @property
    def repo(self) -> PromotionAdminRepo:
        if self._repo is None:
            self._repo = PromotionAdminRepo()
        return self._repo

    @property
    def rules(self) -> PromotionRuleAdminRepo:
        if self._rules is None:
            self._rules = PromotionRuleAdminRepo()
        return self._rules

    @property
    def usage_repo(self) -> PromotionUsageAdminRepo:
        if self._usage is None:
            self._usage = PromotionUsageAdminRepo()
        return self._usage

    # ---------- reads ----------

    def list(self, page: PageParams, *, search: Optional[str] = None, status: str = "all",
             rule_ids: Optional[List[str]] = None, bundle_codes: Optional[List[str]] = None,
             include_global: bool = False) -> AdminPromotionPage:
        result = self.repo.search(page, search=search, status=status, rule_ids=rule_ids,
                                  bundle_codes=bundle_codes, include_global=include_global)
        facets = {key: int(result.facets.get(key, 0)) for key in ("all", "active", "inactive", "expired", "scheduled")}
        return AdminPromotionPage(data=[shape_promotion(row) for row in result.rows], total_count=result.total,
                                  facets=facets)

    def get(self, promotion_id: str) -> Dict[str, Any]:
        return shape_promotion(self._row_or_404(promotion_id))

    def usage(self, page: PageParams, *, code: Optional[str] = None, user_id: Optional[str] = None,
              bundle_ids: Optional[List[str]] = None, date_from: Optional[date] = None,
              date_to: Optional[date] = None) -> AdminPromotionUsagePage:
        start, end = day_range(date_from, date_to)
        result = self.usage_repo.search(page, code=code, user_id=user_id, bundle_ids=bundle_ids,
                                        created_from=start, created_to=end)
        return AdminPromotionUsagePage(data=[shape_usage_row(row) for row in result.rows], total_count=result.total,
                                       totals=shape_totals(result.totals))

    # ---------- writes ----------

    def create(self, body: AdminPromotionCreate, ctx: AuditContext) -> Dict[str, Any]:
        """201. 409 duplicate_code / duplicate_name; 422 unknown rule, missing amount, unknown bundle code."""
        rule = self._rule_or_422(str(body.rule_id))
        amount = self._check_amount(rule, body.amount)
        row = {
            "name": body.name,
            "code": body.code,
            "rule_id": str(body.rule_id),
            "amount": amount,
            "valid_from": to_db_timestamp(body.valid_from),
            "valid_to": to_db_timestamp(body.valid_to),
            "bundle_code": join_bundle_codes(body.bundle_codes),
            "type": PROMOTION_TYPE,
            "is_active": True,
            "times_used": 0,
        }
        created = self._write(lambda: self.repo.insert_audited(row, ctx))
        return self.get(created["id"])

    def update(self, promotion_id: str, body: AdminPromotionUpdate, ctx: AuditContext) -> Dict[str, Any]:
        """200 (also when nothing changed). 404; 409 duplicate_*; 409 promotion_used when an immutable field
        changes on a used promotion; 422 unknown bundle code, rule or amount."""
        current = self._row_or_404(promotion_id)
        patch: Dict[str, Any] = {"name": body.name, "bundle_code": join_bundle_codes(body.bundle_codes)}
        if body.valid_to is not None:
            patch["valid_to"] = to_db_timestamp(body.valid_to)

        immutable: Dict[str, Any] = {}
        if body.code is not None and body.code != current.get("code"):
            immutable["code"] = body.code
        if body.rule_id is not None and str(body.rule_id) != str(current.get("rule_id")):
            immutable["rule_id"] = str(body.rule_id)
        if body.amount is not None and float(body.amount) != float(current.get("amount") or 0):
            immutable["amount"] = body.amount
        if body.valid_from is not None and to_db_timestamp(body.valid_from) != _as_db(current.get("valid_from")):
            immutable["valid_from"] = to_db_timestamp(body.valid_from)

        times_used = int(current.get("times_used") or 0)
        if immutable and times_used > 0:
            raise AdminConflict("promotion_used", times_used=times_used,
                                fields=sorted(immutable))
        guard = None
        if immutable:
            rule = self._rule_or_422(immutable.get("rule_id") or str(current.get("rule_id")))
            amount = immutable["amount"] if "amount" in immutable else current.get("amount")
            immutable_amount = self._check_amount(rule, amount)
            if "amount" in immutable or "rule_id" in immutable:
                immutable["amount"] = immutable_amount
            valid_from = _naive(immutable.get("valid_from") or current.get("valid_from"))
            valid_to = _naive(patch.get("valid_to") or current.get("valid_to"))
            if valid_from and valid_to and valid_to <= valid_from:
                raise AdminValidationError("valid_to must be after valid_from")
            patch.update(immutable)
            guard = {"times_used": 0}  # a checkout racing this edit makes it fail (409 promotion_used)
        try:
            self._write(lambda: self.repo.update_audited(promotion_id, patch, ctx, guard=guard))
        except AdminConflict as exc:
            if exc.conflict_code == "guard_failed":
                raise AdminConflict("promotion_used", times_used=times_used or 1, fields=sorted(immutable)) from exc
            if exc.conflict_code != "no_change":
                raise
        return self.get(promotion_id)

    def set_status(self, promotion_id: str, body: AdminStatusPatch, ctx: AuditContext) -> Dict[str, Any]:
        """404 when missing; 409 no_change when already in that state (from the SQL function)."""
        self.repo.update_audited(promotion_id, {"is_active": body.is_active}, ctx)
        return self.get(promotion_id)

    def delete(self, promotion_id: str, ctx: AuditContext) -> None:
        """204. 404 when missing; 409 promotion_used {times_used} when the promotion was used."""
        current = self._row_or_404(promotion_id)
        times_used = int(current.get("times_used") or 0)
        if times_used > 0:
            raise AdminConflict("promotion_used", times_used=times_used)
        try:
            self.repo.delete_audited(promotion_id, ctx, guard={"times_used": 0})
        except AdminConflict as exc:
            # referenced: FK from promotion_usage.promotion_code or user_order.promo_code (a usage was
            # recorded without the counter, or the counter raced the delete); guard_failed: used meanwhile.
            if exc.conflict_code in ("referenced", "guard_failed"):
                raise AdminConflict("promotion_used", times_used=max(times_used, 1)) from exc
            raise

    # ---------- helpers ----------

    def _row_or_404(self, promotion_id: str) -> Dict[str, Any]:
        row = self.repo.get(promotion_id)
        if row is None:
            raise AdminNotFound("not_found")
        return row

    def _rule_or_422(self, rule_id: str) -> Dict[str, Any]:
        rule = self.rules.get(rule_id)
        if rule is None:
            raise AdminValidationError("rule_id: unknown rule")
        return rule

    @staticmethod
    def _check_amount(rule: Dict[str, Any], amount: Optional[float]) -> Optional[float]:
        """Amount-based actions need ``amount``; percentage actions cap it at 100."""
        amount_based = is_amount_action(rule)
        if amount is None:
            if amount_based:
                raise AdminValidationError("amount: required for an amount-based rule")
            return None
        if amount <= 0:
            raise AdminValidationError("amount: must be greater than 0")
        if amount_based is False and amount > MAX_PERCENTAGE:
            raise AdminValidationError("amount: a percentage is at most 100")
        return float(amount)

    @staticmethod
    def _write(call):
        """The ``validate_bundle_codes`` trigger raises 'Invalid bundle code: X' (P0001 -> 422)."""
        try:
            return call()
        except AdminValidationError as exc:
            text = str(exc.details or "")
            if "Invalid bundle code" in text:
                code = text.split(":", 1)[1].strip() if ":" in text else ""
                raise AdminValidationError(f"bundle_codes: unknown bundle code {code}".rstrip()) from exc
            raise


def _as_db(value: Any) -> Optional[str]:
    parsed = _naive(value)
    return parsed.isoformat() if parsed else None
