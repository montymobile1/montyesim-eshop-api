"""Read-only check of the admin portal accounts before the RLS migration (TEOS-268).

Once the RLS section of supabase_ddl.sql is applied, a user JWT reads other users' rows of user_order, user_wallet,
user_wallet_transaction, promotion_usage, device, users_copy and contact_us only when its
``app_metadata.role`` is ``admin`` or ``super_admin`` (the role the admin API also checks). The old
admin portal reads those tables with the signed-in admin's JWT and never checked that role, so an
admin without it would see empty screens once RLS is on.

This lists every auth user that looks like a portal admin (``app_metadata.role`` set, or an email
containing "admin") with their email and role, and flags the ones with no role. Only the email and
the role are printed: no ids, tokens, phone numbers or other metadata.

``--set-role admin|super_admin --email <email>`` sets ``app_metadata.role`` of that one user; GoTrue
merges it into the existing app_metadata. It is a dry run unless ``--apply`` is given. The new role
is in the user's JWT from their next sign-in or token refresh.

Usage:
    venv/bin/python scripts/check_admin_roles.py
    venv/bin/python scripts/check_admin_roles.py --set-role admin --email someone@example.com
    venv/bin/python scripts/check_admin_roles.py --set-role admin --email someone@example.com --apply

Reads SUPABASE_URL and SUPABASE_KEY (service role) from the environment or .env.
"""

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

ADMIN_ROLES = ("admin", "super_admin")  # app.config.admin_permissions ROLE_ADMIN / ROLE_SUPER_ADMIN
DEFAULT_PER_PAGE = 1000
NO_ROLE = "NO ROLE"


@dataclass(frozen=True)
class Candidate:
    email: str
    role: Optional[str]

    @property
    def flag(self) -> str:
        if self.role is None:
            return "!!! NO ROLE: cannot read other users' rows after RLS"
        if self.role not in ADMIN_ROLES:
            return f"!!! UNKNOWN ROLE: not one of {', '.join(ADMIN_ROLES)}"
        return "ok"


def list_all_users(admin: Any, per_page: int = DEFAULT_PER_PAGE) -> List[Any]:
    """Every auth user, page by page until an empty page."""
    users: List[Any] = []
    page = 1
    while True:
        batch = admin.list_users(page=page, per_page=per_page)
        if not batch:
            return users
        users.extend(batch)
        page += 1


def role_of(user: Any) -> Optional[str]:
    role = (getattr(user, "app_metadata", None) or {}).get("role")
    return str(role) if role else None


def portal_admin_candidates(users: List[Any]) -> List[Candidate]:
    """Users with an app_metadata.role, or with "admin" in their email, sorted by email."""
    found = [Candidate(email=user.email or "", role=role_of(user)) for user in users
             if role_of(user) or "admin" in (user.email or "").lower()]
    return sorted(found, key=lambda c: c.email.lower())


def find_by_email(users: List[Any], email: str) -> List[Any]:
    wanted = email.strip().lower()
    return [user for user in users if (user.email or "").lower() == wanted]


def set_role(admin: Any, users: List[Any], email: str, role: str, apply: bool,
             log: Callable[[str], None] = print) -> int:
    """Sets app_metadata.role of the one user with ``email``; 0 when done (or would be), else 1."""
    if role not in ADMIN_ROLES:
        log(f"--set-role must be one of {', '.join(ADMIN_ROLES)}")
        return 2
    matches = find_by_email(users, email)
    if len(matches) != 1:
        log(f"{email}: {'no user' if not matches else f'{len(matches)} users'} with this email, nothing changed")
        return 1
    user = matches[0]
    current = role_of(user) or NO_ROLE
    if current == role:
        log(f"{user.email}: role is already {role}, nothing to do")
        return 0
    if not apply:
        log(f"[dry-run] would set {user.email}: {current} -> {role} (add --apply to change it)")
        return 0
    try:
        admin.update_user_by_id(user.id, {"app_metadata": {"role": role}})
    except Exception as exc:
        log(f"{user.email}: update failed ({type(exc).__name__}), nothing changed")
        return 1
    log(f"{user.email}: {current} -> {role}. Effective from the user's next sign-in or token refresh.")
    return 0


def report(candidates: List[Candidate], total: int, log: Callable[[str], None] = print) -> int:
    """Prints the candidates; the number of flagged ones."""
    log(f"{total} auth users listed, {len(candidates)} look like portal admins")
    width = max([len(c.email) for c in candidates] + [5])
    for candidate in candidates:
        log(f"  {candidate.email:<{width}}  {candidate.role or NO_ROLE:<12}  {candidate.flag}")
    flagged = [c for c in candidates if c.flag != "ok"]
    if flagged:
        log(f"!!! {len(flagged)} flagged: set a role with --set-role admin|super_admin --email <email> "
            f"before applying the RLS section of supabase_ddl.sql, or confirm they are not portal admins")
    return len(flagged)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="List portal admin accounts and their app_metadata.role.")
    parser.add_argument("--set-role", choices=ADMIN_ROLES, help="set app_metadata.role of --email")
    parser.add_argument("--email", help="the user whose role --set-role sets")
    parser.add_argument("--apply", action="store_true", help="with --set-role: change it (default is a dry run)")
    parser.add_argument("--per-page", type=int, default=DEFAULT_PER_PAGE, help="users per admin list page")
    args = parser.parse_args(argv)
    if bool(args.set_role) != bool(args.email):
        parser.error("--set-role and --email go together")
    if args.apply and not args.set_role:
        parser.error("--apply only applies to --set-role")

    from dotenv import load_dotenv
    from supabase import create_client
    from supabase.lib.client_options import SyncClientOptions

    load_dotenv()
    supabase_url, supabase_key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        print("SUPABASE_URL and SUPABASE_KEY (service role) must be set")
        return 2

    admin = create_client(supabase_url, supabase_key,
                          options=SyncClientOptions(auto_refresh_token=False)).auth.admin
    users = list_all_users(admin, per_page=args.per_page)
    if args.set_role:
        return set_role(admin, users, args.email, args.set_role, apply=args.apply)
    report(portal_admin_candidates(users), total=len(users))
    return 0


if __name__ == "__main__":
    sys.exit(main())
