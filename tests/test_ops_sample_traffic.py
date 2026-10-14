import dataclasses
import importlib.util
import json
import re
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ops_sample_traffic.py"
spec = importlib.util.spec_from_file_location("ops_sample_traffic", SCRIPT)
traffic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(traffic)

PASSWORD = "s3cret-sample-pw"
TOKEN = "header.eyJzdWIiOiJ4In0.signature"
ENV = {
    "PEAR_USER_URL": "http://user.test/",
    "PEAR_PATIENT_URL": "http://patient.test",
    "PEAR_ACTIVITY_URL": "http://activity.test",
    "PEAR_SCHEDULER_URL": "http://scheduler.test",
    "PEAR_SAMPLE_EMAIL": "carer@example.com",
}


def _cfg(**changes):
    return dataclasses.replace(traffic.Config.from_env(ENV), password=PASSWORD, **changes)


REAL_CLIENT = httpx.Client


def _client(seen, login_body=None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/v1/login/":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            if form["password"] == PASSWORD:
                return httpx.Response(200, json=login_body or {"access_token": TOKEN, "refresh_token": "r"})
            return httpx.Response(401, json={"detail": "Could not validate credentials of user"})
        if path == "/api/v1/current_user/":
            return httpx.Response(200, json={"userId": "U5"})
        if path.startswith("/ops-debug/"):
            return httpx.Response(500, text="Internal Server Error")
        if "Authorization" not in request.headers:
            return httpx.Response(401, json={"detail": "No authorization token provided"})
        return httpx.Response(200, json={})

    return REAL_CLIENT(transport=httpx.MockTransport(handler))


def test_urls_come_from_env_and_default_to_local_ports():
    cfg = traffic.Config.from_env(ENV)
    assert cfg.user_url == "http://user.test" and cfg.email == "carer@example.com"
    assert cfg.password == ""
    local = traffic.Config.from_env({})
    assert local.patient_url == "http://localhost:8000" and local.user_url == "http://localhost:8001"


def test_password_is_never_read_from_env():
    assert traffic.Config.from_env({**ENV, "PEAR_SAMPLE_PASSWORD": PASSWORD}).password == ""


def test_run_drives_every_step_with_a_request_id_and_user_agent():
    seen = []
    results = traffic.run(_cfg(), _client(seen))
    assert len(results) == len(seen) == 15
    for request in seen:
        assert re.fullmatch(r"sample-\d\d-[0-9a-f]{8}", request.headers["X-Request-ID"])
        assert request.headers["User-Agent"] == traffic.USER_AGENT
    assert [r.status for r in results[:3]] == [401, 401, 200]
    assert str(seen[7].url) == "http://user.test/api/v1/admin/user/U5"
    assert "mask=false" in str(seen[5].url)
    assert "Authorization" not in seen[6].headers
    assert json.loads(seen[10].content) == {"refresh_token": "not-a-real-refresh-token"}


def test_only_reads_and_auth_attempts_are_sent():
    seen = []
    traffic.run(_cfg(), _client(seen))
    writes = [(r.method, r.url.path) for r in seen if r.method != "GET"]
    assert writes == [("POST", "/api/v1/login/")] * 3 + [("POST", "/api/v1/refresh/")]


def test_debug_error_routes_can_be_skipped():
    seen = []
    results = traffic.run(_cfg(debug_routes=False), _client(seen))
    assert len(results) == len(seen) == 11
    assert not any(r.url.path.startswith("/ops-debug/") for r in seen)


def test_report_never_prints_password_or_token(capsys):
    results = traffic.run(_cfg(), _client([]))
    traffic.report(results)
    out = capsys.readouterr().out
    assert PASSWORD not in out and TOKEN not in out
    assert "Sensitive access: mask=false" in out
    assert "15 requests sent" in out and "401: 4" in out


def test_config_repr_hides_the_password():
    assert PASSWORD not in repr(_cfg())


def test_two_factor_account_stops_early():
    seen = []
    with pytest.raises(SystemExit, match="2FA"):
        traffic.run(_cfg(), _client(seen, login_body={"msg": "2FA required", "email": "x"}))
    assert len(seen) == 3


def test_unreachable_service_is_reported_not_raised():
    def handler(request):
        if request.url.host == "activity.test":
            raise httpx.ConnectError("refused")
        if request.url.path == "/api/v1/login/":
            return httpx.Response(200, json={"access_token": TOKEN})
        return httpx.Response(200, json={"userId": "U5"})

    results = traffic.run(_cfg(), REAL_CLIENT(transport=httpx.MockTransport(handler)))
    failed = [r for r in results if r.status is None]
    assert [r.error for r in failed] == ["ConnectError", "ConnectError"]


def test_main_prompts_for_the_password_and_keeps_it_off_the_command_line(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(traffic.httpx, "Client", lambda **kw: _client(seen))
    code = traffic.main(["--yes", "--patient-id", "7"], env=ENV, ask_password=lambda prompt: PASSWORD)
    out = capsys.readouterr().out
    assert code == 0 and len(seen) == 15
    assert PASSWORD not in out and TOKEN not in out
    assert str(seen[4].url) == "http://patient.test/api/v1/patients/7"


def test_main_asks_before_hitting_a_non_local_target(monkeypatch):
    seen = []
    monkeypatch.setattr(traffic.httpx, "Client", lambda **kw: _client(seen))
    assert traffic.main([], env=ENV, ask_password=lambda p: PASSWORD, confirm=lambda p: "n") == 2
    assert seen == []
    assert traffic.main([], env=ENV, ask_password=lambda p: PASSWORD, confirm=lambda p: "y") == 0


def test_cli_flags_override_env():
    args = traffic.parse_args(["--user-url", "https://stg.example/", "--email", "a@b.c"])
    cfg = traffic.Config.from_env(ENV, args)
    assert cfg.user_url == "https://stg.example" and cfg.email == "a@b.c"
    assert cfg.patient_url == "http://patient.test"
