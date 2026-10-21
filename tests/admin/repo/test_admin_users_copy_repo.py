from types import SimpleNamespace
from unittest.mock import MagicMock

from app.repo.admin.users_copy_repo import UsersCopyAdminRepo, user_summary


def test_user_summary_reads_msisdn_from_metadata():
    assert user_summary({"id": "u1", "email": "a@x.com", "metadata": {"msisdn": "+961"}}) == {
        "id": "u1", "email": "a@x.com", "msisdn": "+961"}
    assert user_summary({"id": "u2", "email": None, "metadata": None})["msisdn"] is None


def test_by_ids_deduplicates_skips_none_and_maps_by_id(monkeypatch):
    repo = UsersCopyAdminRepo()
    queries = []

    def fake_run(call):
        return SimpleNamespace(data=[{"id": "u1", "email": "a@x.com", "metadata": {}}])

    original_filters = repo.filters

    def spy_filters(query, **kwargs):
        queries.append(kwargs)
        return original_filters(query, **kwargs)

    monkeypatch.setattr(repo, "_run", fake_run)
    monkeypatch.setattr(repo, "filters", spy_filters)

    result = repo.by_ids(["u1", None, "u1", "u2"])

    assert queries == [{"in_": {"id": ["u1", "u2"]}}]
    assert result == {"u1": {"id": "u1", "email": "a@x.com", "msisdn": None}}


def test_by_ids_with_no_ids_makes_no_query(monkeypatch):
    repo = UsersCopyAdminRepo()
    monkeypatch.setattr(repo, "_run", MagicMock())

    assert repo.by_ids([None, ""]) == {}
    repo._run.assert_not_called()


def test_lookup_by_id_filters_on_that_id(monkeypatch):
    from app.schemas.admin.common import PageParams
    repo = UsersCopyAdminRepo()
    captured = {}

    def fake_paged(query, page, order):
        captured["params"] = dict(query.params.multi_items())
        from app.repo.admin.base import Paged
        return Paged(rows=[], total=0)

    monkeypatch.setattr(repo, "paged", fake_paged)
    repo.lookup(None, PageParams(), record_id="u1")

    assert captured["params"]["id"] == "eq.u1"
    assert "email" in captured["params"]  # still only users with an email
