"""scripts/rotate_phone_user_passwords.py, against an in-memory stand-in for the GoTrue admin API.

The stand-in reproduces the GoTrue v2.197.0 behaviour the script relies on (checked against a
local GoTrue v2.197.0): an admin password update revokes every session / refresh token of the
user, and an app_metadata update is merged into the existing app_metadata.
"""

import secrets
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import rotate_phone_user_passwords as script


class FakeAdmin:
    def __init__(self, users, fail_on=()):
        # users: list of dicts(id, email, user_metadata, app_metadata)
        self.users = {u["id"]: {**u, "password": u.get("password", "old"), "sessions": {"s1", "s2"}} for u in users}
        self.fail_on = set(fail_on)
        self.updates = []

    def list_users(self, page=None, per_page=None):
        ids = sorted(self.users)
        start = (page - 1) * per_page
        return [SimpleNamespace(id=uid, email=self.users[uid]["email"],
                                user_metadata=dict(self.users[uid].get("user_metadata") or {}),
                                app_metadata=dict(self.users[uid].get("app_metadata") or {}))
                for uid in ids[start:start + per_page]]

    def update_user_by_id(self, uid, attributes):
        if uid in self.fail_on:
            raise RuntimeError("admin api down")
        self.updates.append((uid, attributes))
        user = self.users[uid]
        if "password" in attributes:
            user["password"] = attributes["password"]
            user["sessions"] = set()  # GoTrue revokes all sessions on a password change
        user["app_metadata"] = {**(user.get("app_metadata") or {}), **attributes.get("app_metadata", {})}


def phone(uid, number, **extra):
    return {"id": uid, "email": f"{number}_user@esim.com", "password": f"static_password_{number}",
            "user_metadata": {"msisdn": number, "login_type": "phone"}, **extra}


def population():
    return [
        phone("u1", "+96170000001"),
        phone("u2", "+96170000002"),
        phone("u3", "+447700900003"),
        # phone login done with a real email: same static password, so it is rotated too
        {"id": "u4", "email": "someone@example.com", "user_metadata": {"msisdn": "+96170000004", "login_type": "phone"}},
        # email users
        {"id": "e1", "email": "email.user@example.com", "user_metadata": {"login_type": "email"}},
        {"id": "e2", "email": "no.meta@example.com", "user_metadata": {}},
        # synthetic-looking email but no metadata: skipped (as specified)
        {"id": "n1", "email": "+96170000009_user@esim.com", "user_metadata": {}},
        # non-synthetic email without login_type=phone
        {"id": "n2", "email": "+96170000010_user@other.com", "user_metadata": {"msisdn": "+96170000010"}},
        # anonymous user (no email)
        {"id": "a1", "email": None, "user_metadata": {"email": "guest@example.com"}},
    ]


PHONE_IDS = {"u1", "u2", "u3", "u4"}


def run(admin, apply=True, **kwargs):
    users = script.list_all_users(admin, per_page=2)
    return script.rotate_users(admin, users, apply=apply, log=lambda _: None, **kwargs)


def test_07_sets_random_password_of_at_least_32_bytes_for_every_phone_user():
    admin = FakeAdmin(population())
    sizes = []
    real = secrets.token_urlsafe

    def spy(nbytes=None):
        sizes.append(nbytes)
        return real(nbytes)

    with patch.object(script.secrets, "token_urlsafe", side_effect=spy):
        counts = run(admin)

    assert counts.rotated == 4
    assert sizes == [32] * 4
    assert {uid for uid, _ in admin.updates} == PHONE_IDS
    for uid in PHONE_IDS:
        password = admin.users[uid]["password"]
        assert len(password) >= 43 and not password.startswith("static_password_")


def test_08_each_user_gets_a_different_password():
    admin = FakeAdmin(population())
    run(admin)

    passwords = [admin.users[uid]["password"] for uid in PHONE_IDS]
    assert len(set(passwords)) == len(passwords)


def test_09_revokes_all_existing_sessions_of_each_phone_user():
    admin = FakeAdmin(population())
    run(admin)

    for uid in PHONE_IDS:
        assert admin.users[uid]["sessions"] == set()
    for uid in {"e1", "e2", "n1", "n2", "a1"}:
        assert admin.users[uid]["sessions"] == {"s1", "s2"}
    # revocation is the password update itself; password + marker go in one call
    for _, attributes in admin.updates:
        assert set(attributes) == {"password", "app_metadata"}
        assert script.ROTATED_MARKER in attributes["app_metadata"]


def test_10_skips_email_non_synthetic_and_no_metadata_users_and_reports_counts(capsys):
    admin = FakeAdmin(population())
    counts = run(admin)

    assert (counts.rotated, counts.skipped, counts.already_done, counts.failed) == (4, 5, 0, 0)
    for uid in {"e1", "e2", "n1", "n2", "a1"}:
        assert admin.users[uid]["password"] == "old"
        assert script.ROTATED_MARKER not in (admin.users[uid].get("app_metadata") or {})


def test_11_rerun_is_safe_and_rotates_nobody_twice():
    admin = FakeAdmin(population())
    run(admin)
    passwords = {uid: admin.users[uid]["password"] for uid in PHONE_IDS}
    updates = len(admin.updates)

    counts = run(admin)

    assert (counts.rotated, counts.already_done, counts.failed) == (0, 4, 0)
    assert len(admin.updates) == updates
    assert passwords == {uid: admin.users[uid]["password"] for uid in PHONE_IDS}


def test_24_partial_failure_rerun_finishes_the_rest_without_rerotating():
    admin = FakeAdmin(population(), fail_on={"u2"})
    first = run(admin)
    assert (first.rotated, first.failed, first.failed_user_ids) == (3, 1, ["u2"])
    assert admin.users["u2"]["password"] == "static_password_+96170000002"
    done_passwords = {uid: admin.users[uid]["password"] for uid in PHONE_IDS - {"u2"}}

    admin.fail_on.clear()
    second = run(admin)

    assert (second.rotated, second.already_done, second.failed) == (1, 3, 0)
    assert [uid for uid, _ in admin.updates[-1:]] == ["u2"]
    assert admin.users["u2"]["password"] != "static_password_+96170000002"
    assert done_passwords == {uid: admin.users[uid]["password"] for uid in PHONE_IDS - {"u2"}}


def test_dry_run_changes_nothing():
    admin = FakeAdmin(population())
    counts = run(admin, apply=False)

    assert counts.rotated == 4  # would rotate
    assert admin.updates == []


def test_pagination_reads_every_page():
    admin = FakeAdmin(population())
    assert {u.id for u in script.list_all_users(admin, per_page=2)} == set(admin.users)


def test_synthetic_email_regex_matches_how_auth_service_builds_it():
    phone_number = "+96170123456"
    assert script.SYNTHETIC_EMAIL_REGEX.match(f"{phone_number}_user@esim.com")
    assert not script.SYNTHETIC_EMAIL_REGEX.match("someone@esim.com")
    assert not script.SYNTHETIC_EMAIL_REGEX.match(f"{phone_number}_user@esim.com.evil")


def make_main_env(monkeypatch, admin, version):
    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_KEY", "service-role")
    client = SimpleNamespace(auth=SimpleNamespace(admin=admin))
    return patch("supabase.create_client", return_value=client), \
        patch.object(script, "gotrue_version", return_value=version), \
        patch("dotenv.load_dotenv")


def test_main_defaults_to_dry_run_and_never_prints_passwords(monkeypatch, capsys):
    admin = FakeAdmin(population())
    create, version, dotenv = make_main_env(monkeypatch, admin, (2, 197, 0))
    with create, version, dotenv:
        assert script.main([]) == 0
    assert admin.updates == []
    assert "would_rotate=4 skipped=5 already_done=0 failed=0" in capsys.readouterr().out

    with create, version, dotenv:
        assert script.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "rotated=4 skipped=5 already_done=0 failed=0" in out
    for uid in PHONE_IDS:
        assert admin.users[uid]["password"] not in out


def test_main_apply_exits_nonzero_when_a_user_failed(monkeypatch, capsys):
    admin = FakeAdmin(population(), fail_on={"u1"})
    create, version, dotenv = make_main_env(monkeypatch, admin, (2, 197, 0))
    with create, version, dotenv:
        assert script.main(["--apply"]) == 1
    assert "failed=1" in capsys.readouterr().out


@pytest.mark.parametrize("version", [None, (2, 196, 9), (1, 999, 0)])
def test_main_refuses_apply_on_unverified_gotrue(monkeypatch, version):
    admin = FakeAdmin(population())
    create, gotrue, dotenv = make_main_env(monkeypatch, admin, version)
    with create, gotrue, dotenv:
        assert script.main(["--apply"]) == 2
    assert admin.updates == []


def test_parse_version():
    assert script.parse_version("v2.197.0") == (2, 197, 0)
    assert script.parse_version("2.200.1-rc") == (2, 200, 1)
    assert script.parse_version("nonsense") is None
