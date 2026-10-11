import json

import pytest

from app.crud import logs_crud
from app.schemas.log_query import LogQuery

FUNCS = [
    logs_crud.get_logs_by_param_patient,
    logs_crud.get_logs_by_param_activity,
    logs_crud.get_logs_by_param_user,
    logs_crud.get_logs_by_param_system,
]

LEGACY_OR_ENV = {
    "bool": {
        "should": [
            {"match_phrase": {"fields.environment": "staging"}},
            {"bool": {"must_not": [{"exists": {"field": "fields.environment"}}]}},
        ],
        "minimum_should_match": 1,
    }
}


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    def fake_search(index=None, body=None, headers=None, **kw):
        seen["body"] = body
        return {"hits": {"total": {"value": 0}, "hits": []}}

    monkeypatch.setattr(logs_crud.es_service, "search_documents", fake_search)
    return seen


def _must(captured):
    return captured["body"]["query"]["bool"]["must"]


@pytest.mark.parametrize("func", FUNCS)
def test_explicit_environment_is_exact_match(monkeypatch, captured, func):
    monkeypatch.setenv("LOG_ENVIRONMENT", "staging")
    func(LogQuery(environment="production"))
    must = _must(captured)
    assert {"match_phrase": {"fields.environment": "production"}} in must
    assert LEGACY_OR_ENV not in must


@pytest.mark.parametrize("func", FUNCS)
def test_log_environment_scopes_and_keeps_untagged(monkeypatch, captured, func):
    monkeypatch.setenv("LOG_ENVIRONMENT", " Staging ")
    func(LogQuery())
    assert LEGACY_OR_ENV in _must(captured)


@pytest.mark.parametrize("func", FUNCS)
def test_unset_means_no_environment_condition(monkeypatch, captured, func):
    monkeypatch.delenv("LOG_ENVIRONMENT", raising=False)
    func(LogQuery())
    assert "fields.environment" not in json.dumps(captured["body"])


def _paths(captured):
    return [c["wildcard"]["log.file.path"] for c in _flatten(_must(captured)) if "wildcard" in c]


def _flatten(conds):
    for c in conds:
        yield c
        if "bool" in c:
            for k in ("must", "should"):
                yield from _flatten(c["bool"].get(k, []))


def test_patient_default_only_patient_wildcard(captured):
    logs_crud.get_logs_by_param_patient(LogQuery(patient="42"))
    assert _paths(captured) == ["*pear_patient_service*"]


def test_patient_include_activity_adds_activity_wildcard(captured):
    logs_crud.get_logs_by_param_patient(LogQuery(patient="42"), include_activity=True)
    assert sorted(_paths(captured)) == ["*pear_activity_service*", "*pear_patient_service*"]
    assert {
        "bool": {
            "should": [
                {"wildcard": {"log.file.path": "*pear_patient_service*"}},
                {"wildcard": {"log.file.path": "*pear_activity_service*"}},
            ],
            "minimum_should_match": 1,
        }
    } in _must(captured)
