"""Security pass, 2026-09-26.

The service is public (Slack's /crawl and eBay's account-deletion callback need
it to be), and three routes trusted that nobody would find them:

* /run-scan checked nothing -- anyone could start ?year_crawl / ?ignore_seen.
* /test-clip checked nothing and fetched any URL it was given, unbounded, then
  ran CLIP on it.
* every internal route also admitted any 127.0.0.1 caller, although every
  self-call sends the secret.

Plus: bucket caches loaded with torch.load(weights_only=False), which unpickles.

main.py imports the heavy stack, so its source is read via ast and the auth
helpers are exec'd with stubs.

Run: python tests/run_security_hardening_tests.py
"""

import ast
import hmac
import os
import sys
import types

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = open(os.path.join(PKG, "main.py")).read()
_TREE = ast.parse(_SRC)


def _func(name):
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined in main.py")


def _src(name):
    return ast.get_source_segment(_SRC, _func(name)) or ""


def _exec(names, g):
    exec(compile(ast.Module(body=[_func(n) for n in names], type_ignores=[]),
                 "<main.py>", "exec"), g)
    return g


def _routes():
    out = []
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "route" and dec.args
                    and isinstance(dec.args[0], ast.Constant)):
                out.append((dec.args[0].value, node))
    return out


class _Req:
    def __init__(self, headers, host="svc.run.app"):
        self.headers = headers
        self.host = host


# --- every route guards itself ---------------------------------------------

def test_no_route_trusts_the_socket_address():
    for path, fn in _routes():
        assert "remote_addr" not in ast.get_source_segment(_SRC, fn), path


def test_every_non_public_route_checks_auth_first():
    """Only Slack (signed by Bolt), the health probe and eBay's deletion
    callback may be open."""
    public = {"/slack/events", "/health", "/ebay/account-deletion"}
    guards = ("_internal_request_ok(request)", "_operator_request_ok(request)",
              "_run_scan_authorized(request)")
    for path, fn in _routes():
        if path in public:
            continue
        body = ast.get_source_segment(_SRC, fn)
        hit = [body.index(g) for g in guards if g in body]
        assert hit, f"{path} checks no auth"
        for later in ("request.args", "request.get_json", "_ensure_clip_loaded",
                      "_scan_lock"):
            if later in body:
                assert min(hit) < body.index(later), f"{path} acts before auth"


def test_secret_matches_is_exact_and_never_matches_an_unset_secret():
    ok = _exec(["_secret_matches"], {"hmac": hmac})["_secret_matches"]
    assert ok("s3cret", "s3cret") and not ok("s3cret", "s3creT")
    assert not ok("", "") and not ok(None, "x") and not ok("x", None)


def test_operator_and_internal_headers():
    g = _exec(["_secret_matches", "_internal_request_ok", "_operator_request_ok"],
              {"hmac": hmac, "_INTERNAL_SECRET": "int", "_PIPELINE_SHARED_SECRET": "op"})
    assert g["_internal_request_ok"](_Req({"X-Internal-Secret": "int"}))
    assert not g["_internal_request_ok"](_Req({"X-Pipeline-Secret": "op"}))
    assert g["_operator_request_ok"](_Req({"X-Pipeline-Secret": "op"}))
    assert g["_operator_request_ok"](_Req({"X-Internal-Secret": "int"}))
    assert not g["_operator_request_ok"](_Req({}))


# --- /run-scan: the scheduler's token -------------------------------------------

SA = "scout@proj.iam.gserviceaccount.com"


def _run_scan_ok(headers, claims=None, env=None, runtime_sa=SA, raises=None,
                 service_url="https://svc.run.app", host="svc.run.app",
                 config_sa=""):
    def verify(token, request):
        if raises:
            raise raises
        return dict(claims or {})

    fake = {n: types.ModuleType(n) for n in (
        "google", "google.oauth2", "google.oauth2.id_token", "google.auth",
        "google.auth.transport", "google.auth.transport.requests")}
    fake["google.oauth2.id_token"].verify_oauth2_token = verify
    fake["google.oauth2"].id_token = fake["google.oauth2.id_token"]
    fake["google.auth.transport.requests"].Request = lambda: None
    fake["google.auth.transport"].requests = fake["google.auth.transport.requests"]
    saved = {k: sys.modules.get(k) for k in fake}
    sys.modules.update(fake)
    try:
        g = _exec(["_secret_matches", "_internal_request_ok", "_operator_request_ok",
                   "_scheduler_token_ok", "_run_scan_authorized"], {
            "hmac": hmac, "_INTERNAL_SECRET": "int", "_PIPELINE_SHARED_SECRET": "op",
            "_SERVICE_URL": service_url, "_runtime_sa_email": lambda: runtime_sa,
            "os": types.SimpleNamespace(environ=env or {}),
            "config": types.SimpleNamespace(SCHEDULER_SA_EMAIL=config_sa),
            "print": lambda *a, **k: None})
        return g["_run_scan_authorized"](_Req(headers, host))
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


GOOD = {"email": SA, "email_verified": True, "aud": "https://svc.run.app"}
BEARER = {"Authorization": "Bearer tok"}


def test_run_scan_refuses_an_anonymous_call():
    """The hole: a bare POST started the daily scan, or any of its crawls."""
    assert _run_scan_ok({}) is False


def test_the_scheduler_token_is_accepted():
    assert _run_scan_ok(BEARER, claims=GOOD) is True


def test_the_operator_shared_secret_is_accepted():
    assert _run_scan_ok({"X-Pipeline-Secret": "op"}) is True


def test_any_other_google_account_is_refused():
    assert _run_scan_ok(BEARER, claims=dict(GOOD, email="someone@gmail.com")) is False
    assert _run_scan_ok(BEARER, claims=dict(GOOD, email_verified=False)) is False


def test_a_token_for_another_audience_is_refused():
    assert _run_scan_ok(BEARER, claims=dict(GOOD, aud="https://evil.example"),
                        host="svc.run.app") is False


def test_the_request_host_counts_as_this_service():
    """The scheduler's audience is the URL it calls, which can differ from the
    SERVICE_URL env (Cloud Run has two URL forms)."""
    assert _run_scan_ok(BEARER, claims=dict(GOOD, aud="https://other-form.run.app"),
                        host="other-form.run.app", service_url="https://svc.run.app")


def test_the_schedulers_default_audience_is_accepted():
    """A job created without --oidc-token-audience uses its target URL."""
    assert _run_scan_ok(BEARER, claims=dict(GOOD, aud="https://svc.run.app/run-scan"))


def test_scheduler_sa_env_overrides_the_runtime_account():
    other = "sched@proj.iam.gserviceaccount.com"
    env = {"SCHEDULER_SA_EMAIL": other}
    assert _run_scan_ok(BEARER, claims=dict(GOOD, email=other), env=env) is True
    assert _run_scan_ok(BEARER, claims=GOOD, env=env) is False


def test_the_env_var_replaces_the_config_account_too():
    cfg = "cfg@proj.iam.gserviceaccount.com"
    env = {"SCHEDULER_SA_EMAIL": "sched@proj.iam.gserviceaccount.com"}
    assert _run_scan_ok(BEARER, claims=dict(GOOD, email=cfg), env=env,
                        config_sa=cfg) is False


def test_no_known_scheduler_account_refuses_every_token():
    assert _run_scan_ok(BEARER, claims=GOOD, runtime_sa="") is False


def test_the_live_scheduler_job_is_accepted():
    """The live ebay-scout-daily job, as `gcloud scheduler jobs describe`
    reported it on 2026-09-27: it signs as a DEDICATED account, not the
    service's runtime account, with the service URL as audience, calling
    /run-scan on that URL.  The first cut of this check trusted only the
    runtime account, so the 9 AM scan would have gone 403 on deploy."""
    sys.path.insert(0, os.path.dirname(PKG))
    from ebayscout import config
    job_sa = "ebay-scout-scheduler@project-60d488c5-9c8e-4acc-aac.iam.gserviceaccount.com"
    job_aud = "https://ebay-scout-404960106109.us-east1.run.app"
    assert config.SCHEDULER_SA_EMAIL == job_sa
    build = open(os.path.join(PKG, "cloudbuild.yaml")).read()
    assert f"SERVICE_URL={job_aud}" in build, "SERVICE_URL no longer the job's audience"
    claims = {"email": job_sa, "email_verified": True, "aud": job_aud}
    assert _run_scan_ok(BEARER, claims=claims, config_sa=config.SCHEDULER_SA_EMAIL,
                        runtime_sa="404960106109-compute@developer.gserviceaccount.com",
                        service_url=job_aud,
                        host="ebay-scout-404960106109.us-east1.run.app") is True
    # ...and with no SERVICE_URL at all, the host the job calls still matches.
    assert _run_scan_ok(BEARER, claims=claims, config_sa=config.SCHEDULER_SA_EMAIL,
                        runtime_sa="", service_url="",
                        host="ebay-scout-404960106109.us-east1.run.app") is True


def test_an_unverifiable_token_is_refused():
    assert _run_scan_ok(BEARER, raises=ValueError("bad signature")) is False


# --- /test-clip -------------------------------------------------------------

def test_test_clip_uses_the_capped_downloader():
    body = _src("test_clip")
    assert "_ip.download_image(" in body
    assert "req.get(" not in body and "requests.get(" not in body


def test_download_image_stops_at_the_cap():
    sys.path.insert(0, os.path.dirname(PKG))
    from ebayscout import image_proc

    class _Resp:
        def __init__(self, n, declared=None):
            self.n, self.headers, self.closed = n, {}, False
            if declared is not None:
                self.headers["Content-Length"] = str(declared)

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            sent = 0
            while sent < self.n:
                k = min(chunk_size, self.n - sent)
                sent += k
                yield b"x" * k

        def close(self):
            self.closed = True

    real = image_proc.requests.get
    try:
        image_proc.requests.get = lambda *a, **k: _Resp(1000)
        assert image_proc.download_image("u", max_bytes=2000) == b"x" * 1000
        big = _Resp(10_000)
        image_proc.requests.get = lambda *a, **k: big
        try:
            image_proc.download_image("u", max_bytes=2000)
            raise AssertionError("no cap")
        except ValueError:
            assert big.closed
        image_proc.requests.get = lambda *a, **k: _Resp(10, declared=10**12)
        try:
            image_proc.download_image("u", max_bytes=2000)
            raise AssertionError("declared size ignored")
        except ValueError:
            pass
    finally:
        image_proc.requests.get = real


# --- bucket caches ------------------------------------------------------------

def test_bucket_caches_are_never_unpickled():
    for rel in ("clip_matcher.py", os.path.join("tools", "audit_reference_coverage.py")):
        src = open(os.path.join(PKG, rel)).read()
        assert "weights_only=False" not in src, rel
        assert "weights_only=True" in src, rel
