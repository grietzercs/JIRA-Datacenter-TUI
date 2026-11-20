import base64
import json
import logging
import os
import stat

from jiratui import config, redact
from jiratui.api import BasicAuth, BearerTokenAuth, JiraClient
from jiratui.logsetup import setup_logging
from jiratui.testing.fake_server import DEMO_PASSWORD, DEMO_TOKEN, DEMO_URL, FakeJira


def test_redact_patterns():
    s = "Authorization: Bearer abcdefghijklmnop and Basic dXNlcjpwYXNzd29yZA== https://u:secretpw@host/x"
    out = redact.redact(s)
    assert "abcdefghijklmnop" not in out
    assert "dXNlcjpwYXNzd29yZA==" not in out
    assert "secretpw" not in out


def test_registered_secrets_are_redacted():
    BasicAuth("bob", "hunter2-pat")
    text = "token hunter2-pat and b64 " + base64.b64encode(b"bob:hunter2-pat").decode()
    out = redact.redact(text)
    assert "hunter2-pat" not in out
    assert base64.b64encode(b"bob:hunter2-pat").decode() not in out


def test_redact_headers():
    h = redact.redact_headers({"Authorization": "Bearer x", "Accept": "application/json", "Cookie": "JSESSIONID=b"})
    assert h["Authorization"] == redact.REDACTED and h["Cookie"] == redact.REDACTED
    assert h["Accept"] == "application/json"


async def test_debug_log_never_contains_token(tmp_path):
    log_path = tmp_path / "debug.log"
    setup_logging(str(log_path))
    try:
        fake = FakeJira()
        async with JiraClient(DEMO_URL, BearerTokenAuth(DEMO_TOKEN), transport=fake.transport()) as c:
            await c.test_connection()
            await c.search("project = PLAT", 0, 5)
        async with JiraClient(DEMO_URL, BasicAuth("alice", DEMO_PASSWORD), transport=fake.transport()) as c:
            await c.myself()
        # Also try logging the secrets directly: they must be scrubbed.
        logging.getLogger("jiratui.test").debug("oops %s %s", DEMO_TOKEN, DEMO_PASSWORD)
        for h in logging.getLogger("jiratui").handlers:
            h.flush()
        text = log_path.read_text()
        assert "GET /rest/api/2/myself" in text and "POST /rest/api/2/search" in text
        assert DEMO_TOKEN not in text and DEMO_PASSWORD not in text
        assert base64.b64encode(f"alice:{DEMO_PASSWORD}".encode()).decode() not in text
        assert redact.REDACTED in text
        assert stat.S_IMODE(os.stat(log_path).st_mode) == 0o600
    finally:
        setup_logging(None)


async def test_error_messages_are_redacted():
    from jiratui.tui.common import error_text

    BearerTokenAuth("super-secret-token-1")
    from jiratui.api.errors import JiraError

    assert "super-secret-token-1" not in error_text(JiraError("echoed super-secret-token-1 back"))


def test_parse_dotenv():
    text = """
# comment
export JIRA_URL=https://jira.example.test
JIRA_PAT="abc 123"
JIRA_USER='alice'
EMPTY=
WITH_COMMENT=value # trailing
"""
    env = config.parse_dotenv(text)
    assert env == {
        "JIRA_URL": "https://jira.example.test",
        "JIRA_PAT": "abc 123",
        "JIRA_USER": "alice",
        "EMPTY": "",
        "WITH_COMMENT": "value",
    }


def test_credentials_from_env():
    c = config.credentials_from_env({"JIRA_URL": "https://x", "JIRA_PAT": "tok"})
    assert c.auth_method == "bearer" and c.secret == "tok"
    assert "tok" not in repr(c)
    c = config.credentials_from_env({"JIRA_TOKEN": "tok2"})
    assert c.auth_method == "bearer" and c.secret == "tok2"
    c = config.credentials_from_env({"JIRA_USER": "u", "JIRA_PASSWORD": "pw"})
    assert c.auth_method == "basic" and c.secret == "pw"
    c = config.credentials_from_env({"JIRA_AUTH": "basic", "JIRA_USERNAME": "u", "JIRA_PAT": "tok"})
    assert c.auth_method == "basic" and c.secret == "tok" and c.username == "u"
    c = config.credentials_from_env({"JIRA_CA_BUNDLE": "~/ca.pem"})
    assert c.ca_bundle == "~/ca.pem"


def test_saved_config_never_contains_secrets(tmp_path):
    s = config.Settings(base_url="https://jira", username="alice", remember=True, last_project="PLAT")
    s.secret = "should-not-persist"  # type: ignore[attr-defined]  # even if someone attaches one
    path = config.save(s)
    data = json.loads(open(path).read())
    assert "should-not-persist" not in json.dumps(data)
    assert set(data) <= set(config.Settings.PERSISTED)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    loaded = config.load_saved()
    assert loaded.base_url == "https://jira" and loaded.username == "alice" and loaded.last_project == "PLAT"


def test_tls_options_build(tmp_path):
    import ssl

    import pytest

    from jiratui.api.tls import TLSOptions, build_verify

    assert build_verify(TLSOptions(verify=False)) is False
    ctx = build_verify(TLSOptions())
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED
    with pytest.raises(FileNotFoundError):
        build_verify(TLSOptions(ca_bundle=str(tmp_path / "missing.pem")))
