"""The non-secret IDs are read from a plain env var before Secret Manager.

Secret Manager bills every stored secret version past the free six.  The Slack
channel ID and the two Google Sheet keys are not credentials, so _get_secret
returns a plain Cloud Run env var of the same name when one is set and falls back
to Secret Manager when it is not.  Both halves have to hold: the IDs must reach
the env var (once their secrets are deleted, a miss fails the fallback read and
the service does not start), and nothing else may (a token read from a plain env
var is a credential sitting in the service config).

main.py fetches secrets at import time, so _PLAIN_ENV_IDS and _get_secret are
lifted out by ast and run against a fake Secret Manager, the technique
test_seen_writer.py uses.  What runs is the shipped source.

Run: python tests/run_plain_env_ids_tests.py
"""

import ast
import contextlib
import os
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_PY = os.path.join(os.path.dirname(HERE), "main.py")

_SRC = open(MAIN_PY).read()
_TREE = ast.parse(_SRC)

_IDS = {"CHANNEL_ID_EBAY", "SPREADSHEET_ID", "LOGGER_ID"}


def _is_ids_assign(node):
    return isinstance(node, ast.Assign) and any(
        getattr(t, "id", None) == "_PLAIN_ENV_IDS" for t in node.targets)


def _secret_reads(node):
    return [c for c in ast.walk(node)
            if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_get_secret"]


def _load():
    """(_get_secret, _PLAIN_ENV_IDS, reads), with every Secret Manager read
    appended to ``reads``."""
    reads = []

    class _Client:
        def access_secret_version(self, request):
            reads.append(request["name"])
            secret_id = request["name"].split("/secrets/")[1].split("/")[0]
            return SimpleNamespace(
                payload=SimpleNamespace(data=f"sm-{secret_id}".encode()))

    ns = {"os": os,
          "secretmanager": SimpleNamespace(SecretManagerServiceClient=_Client),
          "config": SimpleNamespace(PROJECT_NUMBER="0")}
    for node in _TREE.body:
        if _is_ids_assign(node) or (
                isinstance(node, ast.FunctionDef) and node.name == "_get_secret"):
            exec(ast.get_source_segment(_SRC, node), ns)
    return ns["_get_secret"], ns["_PLAIN_ENV_IDS"], reads


@contextlib.contextmanager
def _env(**values):
    """Set (str) or unset (None) env vars for the block, then restore them."""
    saved = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_the_list_is_exactly_the_ids():
    # Pinned: adding a token here would move a credential into plain config.
    _, ids, _ = _load()
    assert set(ids) == _IDS


def test_every_listed_id_is_one_main_reads():
    # A misspelt name never matches its env var, so it would still need the
    # secret — and fail at startup once the secret is deleted.
    read = {c.args[0].value for c in _secret_reads(_TREE)
            if c.args and isinstance(c.args[0], ast.Constant)}
    _, ids, _ = _load()
    assert set(ids) <= read, set(ids) - read


def test_list_is_defined_before_the_import_time_reads():
    # main.py reads CHANNEL_ID_EBAY while it is being imported; the list has to
    # exist by then or the import raises NameError.
    defined = next(n.lineno for n in _TREE.body if _is_ids_assign(n))
    first_read = min(n.lineno for n in _TREE.body
                     if not isinstance(n, (ast.FunctionDef, ast.ClassDef))
                     and _secret_reads(n))
    assert defined < first_read


def test_env_var_wins_and_secret_manager_is_not_called():
    get, _, reads = _load()
    with _env(CHANNEL_ID_EBAY=" C0123ABC\n"):
        assert get("CHANNEL_ID_EBAY") == "C0123ABC"
    assert reads == []


def test_unset_or_blank_env_var_falls_back_to_secret_manager():
    get, _, reads = _load()
    with _env(SPREADSHEET_ID=None, LOGGER_ID="  "):
        assert get("SPREADSHEET_ID") == "sm-SPREADSHEET_ID"
        assert get("LOGGER_ID") == "sm-LOGGER_ID"
    assert len(reads) == 2


def test_tokens_ignore_a_same_named_env_var():
    get, _, reads = _load()
    with _env(EBAY_BOT_TOKEN="plain-text-token"):
        assert get("EBAY_BOT_TOKEN") == "sm-EBAY_BOT_TOKEN"
    assert len(reads) == 1
