"""TagGroupAdminRepo.get_with_tags (TEOS-180, TEOS-181): the group detail read used by GET and PUT
/groups/{id}. Queries are built with the real PostgREST client (dummy URL) and inspected; nothing is sent.

PostgREST refuses a related order on a to-many embed (PGRST118 "A related order on 'tag' is not
possible"), so the request must not carry ``order=tag(...)``; the tags are sorted after the read.
"""
from types import SimpleNamespace

from postgrest._sync.request_builder import SyncSelectRequestBuilder

from app.repo.admin.tag_group_repo import TagGroupAdminRepo


def _capture(monkeypatch, rows):
    seen = {}

    def fake_execute(self):
        seen["params"] = dict(self.params.multi_items())
        return SimpleNamespace(data=rows)

    monkeypatch.setattr(SyncSelectRequestBuilder, "execute", fake_execute)
    return seen


def _tag(name, sorting_number):
    return {"id": name, "name": name, "sorting_number": sorting_number}


def test_the_request_has_no_related_order_on_the_tags(monkeypatch):
    seen = _capture(monkeypatch, [{"id": 2, "name": "Regions", "tag": []}])

    TagGroupAdminRepo().get_with_tags(2)

    assert seen["params"] == {"select": "*,tag(*)", "id": "eq.2", "limit": "1"}
    assert not any(key.endswith("order") for key in seen["params"])


def test_tag_search_filters_the_embedded_tags_only(monkeypatch):
    seen = _capture(monkeypatch, [{"id": 1, "name": "Countries", "tag": []}])

    TagGroupAdminRepo().get_with_tags(1, " leb ")

    assert seen["params"]["tag.name"] == "ilike.%leb%"
    assert "order" not in seen["params"]


def test_tags_come_back_ordered_by_sorting_number_then_name(monkeypatch):
    tags = [_tag("Zed", 2), _tag("beta", None), _tag("Alpha", None), _tag("Yemen", 1), _tag("Andorra", 2)]
    _capture(monkeypatch, [{"id": 7, "name": "Regions", "tag": tags}])

    row = TagGroupAdminRepo().get_with_tags(7)

    # sorting_number first (none = the 10000 default, last), then name ignoring case
    assert [tag["name"] for tag in row["tag"]] == ["Yemen", "Andorra", "Zed", "Alpha", "beta"]
    assert row["name"] == "Regions"


def test_unknown_group_is_none(monkeypatch):
    _capture(monkeypatch, [])

    assert TagGroupAdminRepo().get_with_tags(999999) is None


def test_a_group_without_tags_keeps_an_empty_list(monkeypatch):
    _capture(monkeypatch, [{"id": 3, "name": "Empty", "tag": None}])

    assert TagGroupAdminRepo().get_with_tags(3)["tag"] == []
