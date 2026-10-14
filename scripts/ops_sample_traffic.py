"""Drive a little real traffic through a PEAR stack so the admin console's
Errors, Requests, Auth events and Sensitive access pages have something to show.

Only for a local or staging stack where OPS_LOGGING=true. It reads data and
makes login/refresh attempts; it changes no records (no POST/PUT/PATCH/DELETE
other than login and refresh attempts, and it never touches patient data).
The password is asked for with a hidden prompt: it is never taken from the
command line or an environment variable, never written to disk, and never
printed, and neither is any token. Use a non-admin staff account without 2FA
(for example a caregiver from the dev seed).

Usage (service base URLs from flags or PEAR_*_URL variables; defaults are the
usual local ports):

    # Mac / Linux, against staging
    python3 scripts/ops_sample_traffic.py \
        --user-url http://<staging-host>:<port> \
        --patient-url http://<staging-host>:<port> \
        --activity-url http://<staging-host>:<port> \
        --scheduler-url http://<staging-host>:<port> \
        --email <caregiver-email>

    # or export PEAR_USER_URL, PEAR_PATIENT_URL, PEAR_ACTIVITY_URL,
    # PEAR_SCHEDULER_URL, PEAR_SAMPLE_EMAIL, PEAR_SAMPLE_PATIENT_ID once, then
    python3 scripts/ops_sample_traffic.py

Other flags: --patient-id N (default 1), --skip-debug-routes (do not call
/ops-debug/error; those routes exist only where OPS_DEBUG_ROUTES=true and
answer 404 elsewhere), --yes (skip the confirmation shown for non-local
targets). Needs only httpx (already in requirements.txt). Exit code 0 when
every request got a response, 1 when a service was unreachable, 2 when you
declined the confirmation.
"""
import argparse
import getpass
import os
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Callable, List, Mapping, Optional, Sequence
from urllib.parse import urlparse

import httpx

USER_AGENT = "pear-ops-sample-traffic/1.0"
UNKNOWN_EMAIL = "ops-sample-unknown-user@example.invalid"
WRONG_PASSWORD = "ops-sample-wrong-password"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
DEFAULT_URLS = {
    "user_url": "http://localhost:8001",
    "patient_url": "http://localhost:8000",
    "activity_url": "http://localhost:8002",
    "scheduler_url": "http://localhost:8080",
}
ENV_NAMES = {
    "user_url": "PEAR_USER_URL",
    "patient_url": "PEAR_PATIENT_URL",
    "activity_url": "PEAR_ACTIVITY_URL",
    "scheduler_url": "PEAR_SCHEDULER_URL",
}


@dataclass
class Config:
    user_url: str
    patient_url: str
    activity_url: str
    scheduler_url: str
    email: str = ""
    password: str = field(default="", repr=False)
    patient_id: str = "1"
    debug_routes: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str], args: Optional[argparse.Namespace] = None) -> "Config":
        """Flags beat environment variables, which beat the local defaults.
        The password is deliberately not read here; main() prompts for it."""
        urls = {}
        for key, default in DEFAULT_URLS.items():
            flag = getattr(args, key, None) if args else None
            urls[key] = (flag or env.get(ENV_NAMES[key]) or default).rstrip("/")
        return cls(
            email=(getattr(args, "email", None) if args else None) or env.get("PEAR_SAMPLE_EMAIL", ""),
            patient_id=(getattr(args, "patient_id", None) if args else None) or env.get("PEAR_SAMPLE_PATIENT_ID", "1"),
            debug_routes=not (args.skip_debug_routes if args else False),
            **urls,
        )

    def targets(self) -> List[str]:
        return [self.user_url, self.patient_url, self.activity_url, self.scheduler_url]

    def is_local(self) -> bool:
        return all((urlparse(url).hostname or "") in LOCAL_HOSTS for url in self.targets())


@dataclass
class Result:
    name: str
    expect: str
    status: Optional[int]
    request_id: str
    error: Optional[str] = None


@dataclass
class Runner:
    client: httpx.Client
    results: List[Result] = field(default_factory=list)

    def call(self, name: str, expect: str, method: str, url: str, **kwargs) -> Optional[httpx.Response]:
        request_id = f"sample-{len(self.results) + 1:02d}-{uuid.uuid4().hex[:8]}"
        headers = {"X-Request-ID": request_id, "User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
        try:
            response = self.client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            self.results.append(Result(name, expect, None, request_id, type(exc).__name__))
            return None
        self.results.append(Result(name, expect, response.status_code, request_id))
        return response


def run(cfg: Config, client: httpx.Client) -> List[Result]:
    r = Runner(client)
    login = f"{cfg.user_url}/api/v1/login/"
    r.call("Login, unknown user", "Auth: login_failed unknown_user", "POST", login,
           data={"username": UNKNOWN_EMAIL, "password": WRONG_PASSWORD})
    r.call("Login, wrong password", "Auth: login_failed wrong_password", "POST", login,
           data={"username": cfg.email, "password": WRONG_PASSWORD})
    response = r.call("Login", "Requests: POST /api/v1/login/ 200", "POST", login,
                      data={"username": cfg.email, "password": cfg.password})
    body = {}
    if response is not None and response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            body = {}
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        raise SystemExit("Login failed or needs 2FA; use a non-admin account without 2FA.")
    auth = {"Authorization": f"Bearer {token}"}

    me = r.call("Current user", "Requests: GET /api/v1/current_user/ 200", "GET",
                f"{cfg.user_url}/api/v1/current_user/", headers=auth)
    user_id = "unknown"
    if me is not None and me.status_code == 200:
        try:
            user_id = me.json().get("userId") or "unknown"
        except ValueError:
            pass

    patient = f"{cfg.patient_url}/api/v1/patients"
    r.call("Patient record read", "Sensitive access: patient record read", "GET",
           f"{patient}/{cfg.patient_id}", headers=auth)
    r.call("Unmasked patient list", "Sensitive access: mask=false", "GET", f"{patient}/",
           headers=auth, params={"mask": "false", "pageSize": 5})
    r.call("Patient read without a token", "Auth: http_401", "GET", f"{patient}/{cfg.patient_id}")
    r.call("Admin-only user lookup", "Auth: http_403 (non-admin account)", "GET",
           f"{cfg.user_url}/api/v1/admin/user/{user_id}", headers=auth)
    r.call("Activity list", "Requests: GET /api/v1/activities/", "GET",
           f"{cfg.activity_url}/api/v1/activities/", headers=auth)
    r.call("Medication schedule", "Auth: http_403 unless the account is a SUPERVISOR", "GET",
           f"{cfg.scheduler_url}/MedicationSchedule/get/", headers=auth)
    r.call("Refresh with a bogus token", "Auth: refresh_failed", "POST",
           f"{cfg.user_url}/api/v1/refresh/", json={"refresh_token": "not-a-real-refresh-token"})
    if cfg.debug_routes:
        for service, base in (("User", cfg.user_url), ("Patient", cfg.patient_url),
                              ("Activity", cfg.activity_url), ("Scheduler", cfg.scheduler_url)):
            r.call(f"{service} forced error", "Errors: RuntimeError (404 if OPS_DEBUG_ROUTES is off)", "GET",
                   f"{base}/ops-debug/error")
    return r.results


def report(results: List[Result], out: Callable[[str], None] = print) -> None:
    out(f"{'#':>2}  {'status':>6}  {'request id':<20}  step -> where to look")
    for index, result in enumerate(results, start=1):
        status = str(result.status) if result.status is not None else (result.error or "-")
        out(f"{index:>2}  {status:>6}  {result.request_id:<20}  {result.name} -> {result.expect}")
    counts = Counter(str(r.status) if r.status is not None else "no response" for r in results)
    out("")
    out(f"{len(results)} requests sent. Status counts: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    out("Search the admin console (Errors / Requests / Auth events / Sensitive access) for these request ids, "
        "or filter on the sample- prefix; lines take a few seconds to reach Elasticsearch.")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drive sample traffic for the operational log pages.")
    parser.add_argument("--user-url", dest="user_url", help="User service base URL (PEAR_USER_URL)")
    parser.add_argument("--patient-url", dest="patient_url", help="Patient service base URL (PEAR_PATIENT_URL)")
    parser.add_argument("--activity-url", dest="activity_url", help="Activity service base URL (PEAR_ACTIVITY_URL)")
    parser.add_argument("--scheduler-url", dest="scheduler_url", help="Scheduler base URL (PEAR_SCHEDULER_URL)")
    parser.add_argument("--email", help="Login email (PEAR_SAMPLE_EMAIL); asked for if missing")
    parser.add_argument("--patient-id", dest="patient_id", help="Patient to read (PEAR_SAMPLE_PATIENT_ID, default 1)")
    parser.add_argument("--skip-debug-routes", action="store_true", help="Do not call /ops-debug/error")
    parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation on non-local targets")
    return parser.parse_args(argv)


def main(
    argv: Optional[Sequence[str]] = None,
    env: Optional[Mapping[str, str]] = None,
    ask_password: Callable[[str], str] = getpass.getpass,
    confirm: Callable[[str], str] = input,
) -> int:
    args = parse_args(argv)
    cfg = Config.from_env(os.environ if env is None else env, args)
    print("Targets: " + ", ".join(cfg.targets()))
    if not cfg.is_local() and not args.yes:
        answer = confirm("This is not a local stack. Send sample traffic to it? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled; nothing was sent.")
            return 2
    if not cfg.email:
        cfg = replace(cfg, email=confirm("Login email: ").strip())
    cfg = replace(cfg, password=ask_password(f"Password for {cfg.email} (hidden): "))
    if not cfg.email or not cfg.password:
        raise SystemExit("An email and a password are required.")
    with httpx.Client(timeout=15.0) as client:
        results = run(cfg, client)
    report(results)
    return 1 if any(result.status is None for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
