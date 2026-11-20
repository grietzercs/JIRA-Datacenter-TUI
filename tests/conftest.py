import os

import pytest

from jiratui import redact
from jiratui.api import BearerTokenAuth, JiraClient
from jiratui.testing.fake_server import DEMO_TOKEN, DEMO_URL, FakeJira


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Never touch the user's real config dir or env credentials during tests.
    monkeypatch.setenv("JIRATUI_CONFIG_DIR", str(tmp_path / "config"))
    for k in list(os.environ):
        if k.startswith("JIRA_"):
            monkeypatch.delenv(k, raising=False)
    redact.clear_secrets()
    yield
    redact.clear_secrets()


@pytest.fixture
def fake():
    return FakeJira()


@pytest.fixture
async def client(fake):
    c = JiraClient(DEMO_URL, BearerTokenAuth(DEMO_TOKEN), transport=fake.transport(), backoff=0)
    yield c
    await c.aclose()
