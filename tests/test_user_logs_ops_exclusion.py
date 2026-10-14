from app.crud import logs_crud
from app.schemas.log_query import LogQuery

OPS_EXCLUSION = {"bool": {"must_not": [{"match_phrase": {"schema": "pear-app-v1"}}]}}
USER_PATH = {"wildcard": {"log.file.path": "*pear_user_service*"}}


def _capture(monkeypatch):
    seen = {}

    def fake_search(index=None, body=None, headers=None, **kwargs):
        seen["body"] = body
        return {"hits": {"total": {"value": 0}, "hits": []}}

    monkeypatch.setattr(logs_crud.es_service, "search_documents", fake_search)
    return seen


def test_account_logs_exclude_operational_lines(monkeypatch):
    seen = _capture(monkeypatch)
    logs_crud.get_logs_by_param_user(LogQuery())
    assert OPS_EXCLUSION in seen["body"]["query"]["bool"]["must"]


def test_account_logs_keep_existing_conditions(monkeypatch):
    seen = _capture(monkeypatch)
    logs_crud.get_logs_by_param_user(LogQuery(action="login"))
    must = seen["body"]["query"]["bool"]["must"]
    assert USER_PATH in must
    assert {"match_phrase": {"action": "login"}} in must
    assert must.count(OPS_EXCLUSION) == 1
