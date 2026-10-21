"""One-off: replace the static password of every phone-login user with a random one.

Phone users used to be created with the password `static_password_{phone}`, which let anyone
holding the public anon key sign in as them with a password grant. Phone sign-in no longer
uses a password (see AuthService.__create_phone_user_session), so each phone user gets a
random password that is never stored or printed.

Session revocation: GoTrue's admin API has no "revoke all sessions of user X" call, but an
admin password update deletes all of the user's sessions and refresh tokens. This was checked
against GoTrue v2.197.0 (the version QA runs): after `update_user_by_id(password=...)` every
existing refresh token fails with `refresh_token_not_found`. Updating only metadata does not
revoke anything. The script therefore refuses `--apply` against an older GoTrue.

Each user is rotated with ONE admin call that sets the password and the marker
`app_metadata.password_rotated_at`, so a user is either fully done or untouched. Users that
already carry the marker are skipped, which makes a re-run after a partial failure finish the
rest without rotating anyone twice.

Usage (dry run is the default and changes nothing):
    venv/bin/python scripts/rotate_phone_user_passwords.py
    venv/bin/python scripts/rotate_phone_user_passwords.py --apply

Reads SUPABASE_URL and SUPABASE_KEY (service role) from the environment or .env.
"""

import argparse
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, List, Optional

# Built by AuthService.__handle_phone_login as f"{phone}_user@esim.com"; phones pass
# PHONE_REGEX (default ^\+\d{1,3}\d{4,14}...), so the local part starts with "+".
SYNTHETIC_EMAIL_REGEX = re.compile(r"^\+?\d+_user@esim\.com$")
ROTATED_MARKER = "password_rotated_at"
# Oldest GoTrue on which an admin password update was verified to revoke all sessions.
MIN_VERIFIED_GOTRUE_VERSION = (2, 197, 0)
DEFAULT_PER_PAGE = 200


def generate_random_password() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class Counts:
    rotated: int = 0
    rotated_no_metadata: int = 0
    skipped: int = 0
    already_done: int = 0
    failed: int = 0
    failed_user_ids: List[str] = field(default_factory=list)


def has_synthetic_email(user: Any) -> bool:
    return bool(SYNTHETIC_EMAIL_REGEX.match((user.email or "").lower()))


def is_phone_user(user: Any) -> bool:
    """A phone-login user: any synthetic email, or any email with login_type=phone + msisdn.

    Users created through phone login with a real email (phone + email sent together) got the
    same static password, so they are included. A synthetic email is rotated even without
    metadata: the phone (and so the static password) is in the email itself. This deliberately
    deviates from the story, which skipped them; nobody signs in with that password, so
    rotating it has no business impact and closes the hole.
    """
    if has_synthetic_email(user):
        return True
    metadata = user.user_metadata or {}
    return metadata.get("login_type") == "phone" and bool(metadata.get("msisdn"))


def is_missing_metadata(user: Any) -> bool:
    return not (user.user_metadata or {})


def is_already_rotated(user: Any) -> bool:
    return bool((user.app_metadata or {}).get(ROTATED_MARKER))


def list_all_users(admin: Any, per_page: int = DEFAULT_PER_PAGE) -> List[Any]:
    """Every auth user. Collected up front so marking users while paging cannot shift pages."""
    users: List[Any] = []
    page = 1
    while True:
        batch = admin.list_users(page=page, per_page=per_page)
        if not batch:
            return users
        users.extend(batch)
        page += 1


def rotate_users(admin: Any, users: Iterable[Any], apply: bool,
                 password_factory: Callable[[], str] = generate_random_password,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 log: Callable[[str], None] = print) -> Counts:
    counts = Counts()
    for user in users:
        if not is_phone_user(user):
            counts.skipped += 1
            continue
        if is_already_rotated(user):
            counts.already_done += 1
            continue
        if not apply:
            log(f"[dry-run] would rotate user {user.id}")
            count_rotated(counts, user)
            continue
        try:
            admin.update_user_by_id(user.id, {
                "password": password_factory(),
                "app_metadata": {ROTATED_MARKER: now().isoformat()},
            })
            count_rotated(counts, user)
        except Exception as e:
            counts.failed += 1
            counts.failed_user_ids.append(user.id)
            log(f"failed to rotate user {user.id}: {type(e).__name__}")
    return counts


def count_rotated(counts: Counts, user: Any) -> None:
    if is_missing_metadata(user):
        counts.rotated_no_metadata += 1
    else:
        counts.rotated += 1


def parse_version(version: str) -> Optional[tuple]:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", version or "")
    return tuple(int(part) for part in match.groups()) if match else None


def gotrue_version(supabase_url: str, api_key: str) -> Optional[tuple]:
    import httpx
    response = httpx.get(f"{supabase_url}/auth/v1/health", headers={"apikey": api_key}, timeout=15)
    response.raise_for_status()
    return parse_version(response.json().get("version", ""))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate the static passwords of phone-login users.")
    parser.add_argument("--apply", action="store_true", help="change passwords (default is a dry run)")
    parser.add_argument("--per-page", type=int, default=DEFAULT_PER_PAGE, help="users per admin list page")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv
    from supabase import create_client
    from supabase.lib.client_options import SyncClientOptions

    load_dotenv()
    supabase_url, supabase_key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        print("SUPABASE_URL and SUPABASE_KEY (service role) must be set")
        return 2

    version = gotrue_version(supabase_url, supabase_key)
    print(f"GoTrue version: {'.'.join(map(str, version)) if version else 'unknown'}")
    if args.apply and (version is None or version < MIN_VERIFIED_GOTRUE_VERSION):
        print("Refusing --apply: password update revoking sessions is only verified on GoTrue >= "
              f"{'.'.join(map(str, MIN_VERIFIED_GOTRUE_VERSION))}")
        return 2

    admin = create_client(supabase_url, supabase_key,
                          options=SyncClientOptions(auto_refresh_token=False)).auth.admin
    users = list_all_users(admin, per_page=args.per_page)
    print(f"{'APPLY' if args.apply else 'DRY RUN'}: {len(users)} auth users listed")
    counts = rotate_users(admin, users, apply=args.apply)
    prefix = "" if args.apply else "would_"
    print(f"{prefix}rotated={counts.rotated} {prefix}rotated_no_metadata={counts.rotated_no_metadata} "
          f"skipped={counts.skipped} already_done={counts.already_done} failed={counts.failed}")
    if counts.failed:
        print("Re-run the same command to retry the failed users; rotated users are skipped.")
    return 1 if counts.failed else 0


if __name__ == "__main__":
    sys.exit(main())
