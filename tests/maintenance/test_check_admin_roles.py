"""scripts/check_admin_roles.py, against an in-memory stand-in for the GoTrue admin API
(app_metadata updates are merged into the existing app_metadata, as GoTrue v2.197.0 does)."""
from types import SimpleNamespace

import pytest

from scripts import check_admin_roles as script


class FakeAdmin:
    def __init__(self, users):
        self.users = {u["id"]: u for u in users}
        self.updates = []
        self.pages = []

    def list_users(self, page=None, per_page=None):
        self.pages.append(page)
        ids = sorted(self.users)[(page - 1) * per_page:page * per_page]
        return [SimpleNamespace(id=i, email=self.users[i].get("email"), phone=self.users[i].get("phone"),
                                app_metadata=dict(self.users[i].get("app_metadata") or {}),
                                user_metadata=dict(self.users[i].get("user_metadata") or {})) for i in ids]

    def update_user_by_id(self, uid, attributes):
        self.updates.append((uid, attributes))
        user = self.users[uid]
        user["app_metadata"] = {**(user.get("app_metadata") or {}), **attributes["app_metadata"]}


USERS = [
    {"id": "u1", "email": "boss@example.com", "phone": "+96170000001",
     "app_metadata": {"provider": "email", "role": "super_admin"}},
    {"id": "u2", "email": "portal.admin@example.com", "app_metadata": {"provider": "email"},
     "user_metadata": {"role": "admin"}},  # user_metadata does not count: the user can edit it
    {"id": "u3", "email": "ADMIN2@example.com", "app_metadata": {"role": "admin"}},
    {"id": "u4", "email": "traveller@example.com", "app_metadata": {"provider": "email"}},
    {"id": "u5", "email": None, "phone": "+96170000005", "app_metadata": {"provider": "phone"}},
    {"id": "u6", "email": "ops@example.com", "app_metadata": {"role": "editor"}},
]


@pytest.fixture
def admin():
    return FakeAdmin([dict(u) for u in USERS])


def test_lists_every_page(admin):
    users = script.list_all_users(admin, per_page=2)

    assert len(users) == len(USERS)
    assert admin.pages == [1, 2, 3, 4]


def test_candidates_are_users_with_a_role_or_admin_in_the_email(admin):
    candidates = script.portal_admin_candidates(script.list_all_users(admin, per_page=10))

    assert candidates == [
        script.Candidate("ADMIN2@example.com", "admin"),
        script.Candidate("boss@example.com", "super_admin"),
        script.Candidate("ops@example.com", "editor"),
        script.Candidate("portal.admin@example.com", None),
    ]
    assert [c.flag.split(":")[0] for c in candidates] == ["ok", "ok", "!!! UNKNOWN ROLE", "!!! NO ROLE"]


def test_report_prints_only_email_and_role(admin):
    lines = []
    flagged = script.report(script.portal_admin_candidates(script.list_all_users(admin)), total=6,
                            log=lines.append)

    text = "\n".join(lines)
    assert flagged == 2
    assert "portal.admin@example.com" in text and "NO ROLE" in text
    for private in ("u1", "u2", "+9617", "provider", "traveller@example.com", "user_metadata"):
        assert private not in text


def test_set_role_is_a_dry_run_by_default(admin):
    users = script.list_all_users(admin)
    lines = []

    assert script.set_role(admin, users, "portal.admin@example.com", "admin", apply=False, log=lines.append) == 0
    assert admin.updates == []
    assert "[dry-run] would set portal.admin@example.com: NO ROLE -> admin" in lines[0]


def test_set_role_with_apply_merges_the_role(admin):
    users = script.list_all_users(admin)

    assert script.set_role(admin, users, " Portal.Admin@Example.com ", "admin", apply=True, log=print) == 0
    assert admin.updates == [("u2", {"app_metadata": {"role": "admin"}})]
    assert admin.users["u2"]["app_metadata"] == {"provider": "email", "role": "admin"}


@pytest.mark.parametrize("email,expected", [("nobody@example.com", "no user"),
                                            ("boss@example.com", "already super_admin")])
def test_set_role_changes_nothing_for_unknown_or_unchanged_users(admin, email, expected):
    users = script.list_all_users(admin)
    lines = []

    code = script.set_role(admin, users, email, "super_admin", apply=True, log=lines.append)

    assert admin.updates == []
    assert code == (1 if expected == "no user" else 0)
    assert expected.split()[-1] in lines[0]


def test_set_role_refuses_an_unknown_role(admin):
    assert script.set_role(admin, script.list_all_users(admin), "boss@example.com", "root", apply=True,
                           log=lambda _: None) == 2
    assert admin.updates == []


@pytest.mark.parametrize("argv", [["--set-role", "admin"], ["--email", "a@b.c"], ["--apply"]])
def test_cli_rejects_incomplete_options(argv):
    with pytest.raises(SystemExit):
        script.main(argv)
