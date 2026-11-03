"""QA bugs of epic TEOS-102 on the groups service: TEOS-182 (alternative_country deduplicated),
TEOS-186 (names unique ignoring case)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from postgrest._sync.request_builder import SyncSelectRequestBuilder

from app.exceptions.admin import AdminConflict, AdminValidationError
from app.repo.admin.tag_group_repo import TagGroupAdminRepo
from app.repo.admin.tag_repo import TagAdminRepo
from app.schemas.admin.groups import AdminGroupCreate, AdminGroupUpdate, normalize_alternative_country
from app.services.admin.group_service import GroupAdminService
from app.services.admin.media_storage_service import MediaStorageService

T1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
T2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.fixture
def repo():
    mock = MagicMock(spec=TagGroupAdminRepo)
    mock.name_taken.return_value = False
    mock.insert_group_with_tags.return_value = {"group": {"id": 1}, "tags": []}
    mock.edit_tag_group.return_value = {"group": {"id": 1}, "tags": []}
    return mock


@pytest.fixture
def media():
    fake = MagicMock(spec=MediaStorageService)
    fake.max_bytes = 10
    return fake


@pytest.fixture
def svc(repo, media):
    return GroupAdminService(repo=repo, tags=MagicMock(spec=TagAdminRepo), media=media)


def _create(name, *tag_names, **extra):
    tags = [{"client_key": f"k{i}", "name": tag_name} for i, tag_name in enumerate(tag_names)]
    return AdminGroupCreate.model_validate({"name": name, "type": 1, "group_category": "on_land", "tags": tags,
                                            **extra})


def _update(name, new=(), updated=(), deleted=()):
    return AdminGroupUpdate.model_validate({
        "name": name, "type": 1, "group_category": "on_land",
        "new_tags": [{"client_key": f"n{i}", "name": tag_name} for i, tag_name in enumerate(new)],
        "updated_tags": [{"id": tag_id, "name": tag_name} for tag_id, tag_name in updated],
        "deleted_tag_ids": list(deleted)})


# ---------- TEOS-182: alternative_country ----------

@pytest.mark.parametrize("value, expected", [
    (["Paris", "Paris", "  Paris  ", ""], "Paris"),
    ("Lyon, Lyon ,Lyon", "Lyon"),
    ([" Beirut ", "beirut", "LB", "LB"], "Beirut,LB"),
    (["ME", "gulf", "me", "Gulf "], "ME,gulf"),
    ([], None),
    (None, None),
])
def test_alternative_country_is_trimmed_and_deduplicated_ignoring_case(value, expected):
    assert normalize_alternative_country(value) == expected


# ---------- TEOS-186: names unique ignoring case ----------

def test_create_refuses_a_group_name_taken_in_another_case(svc, repo, media):
    repo.name_taken.return_value = True

    with pytest.raises(AdminConflict) as raised:
        svc.create(_create("COUNTRIES", "France"), {}, MagicMock())

    assert raised.value.conflict_code == "duplicate_group_name"
    repo.name_taken.assert_called_once_with("COUNTRIES", exclude_id=None)
    media.upload.assert_not_called()


def test_create_refuses_two_tags_that_only_differ_by_case(svc, repo):
    with pytest.raises(AdminConflict) as raised:
        svc.create(_create("Regions", "Same", "same "), {}, MagicMock())

    assert raised.value.conflict_code == "duplicate_tag_name"
    repo.insert_group_with_tags.assert_not_called()


def test_update_excludes_the_group_itself_from_the_name_check(svc, repo):
    repo.get_with_tags.return_value = {"id": 7, "name": "Regions", "tag": [{"id": T1, "name": "Asia"}]}

    svc.update(7, _update("regions"), {}, MagicMock())

    repo.name_taken.assert_called_once_with("regions", exclude_id=7)
    repo.edit_tag_group.assert_called_once()


def test_update_refuses_a_new_tag_matching_a_kept_tag_in_another_case(svc, repo):
    repo.get_with_tags.return_value = {"id": 7, "name": "Regions", "tag": [{"id": T1, "name": "Asia"}]}

    with pytest.raises(AdminConflict) as raised:
        svc.update(7, _update("Regions", new=["ASIA"]), {}, MagicMock())

    assert raised.value.conflict_code == "duplicate_tag_name"


def test_update_allows_reusing_the_name_of_a_deleted_or_renamed_tag(svc, repo):
    repo.get_with_tags.return_value = {"id": 7, "name": "Regions",
                                       "tag": [{"id": T1, "name": "Asia"}, {"id": T2, "name": "Africa"}]}

    # T1 is deleted and T2 renamed: "asia" and "AFRICA" are free again
    svc.update(7, _update("Regions", new=["asia", "AFRICA"], updated=[(T2, "North Africa")], deleted=[T1]),
               {}, MagicMock())

    repo.edit_tag_group.assert_called_once()


def test_repo_name_taken_is_an_escaped_case_insensitive_exact_match(monkeypatch):
    seen = {}

    def fake_execute(self):
        seen["params"] = dict(self.params.multi_items())
        return SimpleNamespace(data=[{"id": 3}])

    monkeypatch.setattr(SyncSelectRequestBuilder, "execute", fake_execute)

    assert TagGroupAdminRepo().name_taken(" 50%_off ", exclude_id=7) is True
    assert seen["params"] == {"select": "id", "name": "ilike.50\\%\\_off", "id": "neq.7", "limit": "1"}
