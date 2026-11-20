import base64
import json
import ssl

import httpx
import pytest
import respx

from jiratui.api import (
    AuthenticationError,
    BadRequestError,
    BasicAuth,
    BearerTokenAuth,
    CaptchaRequiredError,
    ConfigError,
    ConflictError,
    JiraClient,
    JqlError,
    NetworkError,
    NotFoundError,
    Pager,
    PermissionDeniedError,
    TimeoutError_,
    TLSError,
    UnexpectedResponseError,
    make_auth,
    normalize_base_url,
)
from jiratui.api.errors import RateLimitedError, ServerError
from jiratui.api.models import Issue, Page, ServerInfo, parse_sprint_value
from jiratui.testing.fake_server import DEMO_PASSWORD, DEMO_TOKEN, DEMO_URL, FakeJira

BASE = "https://jira.example.test"


def mock(handler, **kw):
    kw.setdefault("backoff", 0)
    return JiraClient(BASE, BearerTokenAuth("t" * 10), transport=httpx.MockTransport(handler), **kw)


# ----------------------------------------------------------------- URL / auth
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://jira.example.test/", "https://jira.example.test"),
        ("  https://JIRA.Example.Test///  ", "https://jira.example.test"),
        ("https://jira.example.test/rest/api/2/serverInfo", "https://jira.example.test"),
        ("https://jira.example.test/browse/PLAT-1", "https://jira.example.test"),
        ("https://jira.example.test/secure/Dashboard.jspa", "https://jira.example.test"),
        ("https://host.test/jira/browse/PLAT-1?x=1", "https://host.test/jira"),
        ("https://host.test/jira/", "https://host.test/jira"),
        ("http://jira.example.test:8080", "http://jira.example.test:8080"),
        ("https://jira.example.test/?foo=1#x", "https://jira.example.test"),
    ],
)
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "jira.example.test", "ftp://jira.example.test", "https://",
                                 "https://user:pw@jira.example.test"])
def test_normalize_base_url_rejects(raw):
    with pytest.raises(ConfigError):
        normalize_base_url(raw)


def test_bearer_header_and_repr():
    a = BearerTokenAuth("tok-123456")
    assert a.headers() == {"Authorization": "Bearer tok-123456"}
    assert "tok-123456" not in repr(a) and "tok-123456" not in str(a)


def test_basic_header():
    a = BasicAuth("mary ann", "p@ss:word")
    expected = "Basic " + base64.b64encode(b"mary ann:p@ss:word").decode()
    assert a.authorization_header() == expected
    assert "p@ss" not in repr(a)


def test_make_auth_validation():
    assert isinstance(make_auth("bearer", secret="x" * 10), BearerTokenAuth)
    assert isinstance(make_auth("pat", secret="x" * 10), BearerTokenAuth)
    assert isinstance(make_auth("basic", username="u", secret="pw12"), BasicAuth)
    with pytest.raises(ValueError):
        make_auth("basic", username="", secret="pw")
    with pytest.raises(ValueError):
        make_auth("bearer", secret="")
    with pytest.raises(ValueError):
        make_auth("kerberos", secret="x")


@respx.mock
async def test_auth_header_sent_bearer_and_basic_but_not_on_probe():
    myself = respx.get(f"{BASE}/rest/api/2/myself").mock(
        return_value=httpx.Response(200, json={"name": "alice", "displayName": "Alice"}))
    info = respx.get(f"{BASE}/rest/api/2/serverInfo").mock(
        return_value=httpx.Response(200, json={"version": "9.4.0", "deploymentType": "Server"}))
    async with JiraClient(BASE, BearerTokenAuth("tok-abcdef")) as c:
        await c.test_connection()
    assert myself.calls.last.request.headers["authorization"] == "Bearer tok-abcdef"
    # The serverInfo probe is anonymous: credentials are only sent once we know it is Jira.
    assert "authorization" not in info.calls.last.request.headers
    async with JiraClient(BASE, BasicAuth("bob", "pw-abcdef")) as c:
        await c.myself()
    assert myself.calls.last.request.headers["authorization"].startswith("Basic ")


# ------------------------------------------------------------- connection
async def test_test_connection_bearer(client):
    info = await client.test_connection()
    assert info.version == "9.12.2" and info.version_numbers == (9, 12, 2)
    assert info.supports_pat is True
    assert info.username == "alice" and info.user.display_name == "Alice Anders"
    assert info.warnings == []


async def test_test_connection_basic_password(fake):
    async with JiraClient(DEMO_URL, BasicAuth("alice", DEMO_PASSWORD), transport=fake.transport()) as c:
        info = await c.test_connection()
    assert info.username == "alice"


async def test_username_mismatch_warning(fake):
    async with JiraClient(DEMO_URL, BearerTokenAuth(DEMO_TOKEN, username="bob"), transport=fake.transport()) as c:
        info = await c.test_connection()
    assert any("not the username entered" in w for w in info.warnings)


async def test_bad_basic_reports_seraph_header(fake):
    async with JiraClient(DEMO_URL, BasicAuth("alice", "wrong"), transport=fake.transport()) as c:
        with pytest.raises(AuthenticationError) as ei:
            await c.test_connection()
    assert ei.value.status == 401
    assert "AUTHENTICATED_FAILED" in ei.value.message
    assert not isinstance(ei.value, CaptchaRequiredError)


async def test_captcha_is_reported(fake):
    fake.captcha_after = 1
    async with JiraClient(DEMO_URL, BasicAuth("alice", "wrong"), transport=fake.transport()) as c:
        with pytest.raises(AuthenticationError):
            await c.myself()
        with pytest.raises(CaptchaRequiredError) as ei:
            await c.myself()
    assert "CAPTCHA_CHALLENGE" in ei.value.message
    assert "browser" in ei.value.hint


async def test_bad_bearer_token(fake):
    async with JiraClient(DEMO_URL, BearerTokenAuth("wrong-token"), transport=fake.transport()) as c:
        with pytest.raises(AuthenticationError) as ei:
            await c.test_connection()
    assert "invalid or expired" in ei.value.message


async def test_old_server_bearer_hint():
    fake = FakeJira(version="8.13.5")
    async with JiraClient(DEMO_URL, BearerTokenAuth(DEMO_TOKEN), transport=fake.transport()) as c:
        info = await c.server_info()
        assert info.supports_pat is False
        with pytest.raises(AuthenticationError) as ei:
            await c.test_connection(info)
    assert "8.14" in ei.value.hint
    # Basic still works on that server.
    async with JiraClient(DEMO_URL, BasicAuth("alice", DEMO_PASSWORD), transport=fake.transport()) as c:
        assert (await c.test_connection()).username == "alice"


def test_server_info_pat_support():
    assert ServerInfo.from_json(BASE, {"version": "8.14.0"}).supports_pat is True
    assert ServerInfo.from_json(BASE, {"version": "8.13.9"}).supports_pat is False
    assert ServerInfo.from_json(BASE, {"version": "10.3.1", "versionNumbers": [10, 3, 1]}).supports_pat is True
    assert ServerInfo.from_json(BASE, {"version": "1001.0.0", "deploymentType": "Cloud"}).supports_pat is False
    assert ServerInfo.from_json(BASE, {"version": "weird"}).supports_pat is None


async def test_200_with_authenticated_failed_header_is_an_auth_error():
    # Jira may serve anonymous-readable data with HTTP 200 although the credentials were rejected.
    def handler(request):
        return httpx.Response(200, json={"name": "anonymous"}, headers={"X-Seraph-LoginReason": "AUTHENTICATED_FAILED"})

    async with mock(handler) as c:
        with pytest.raises(AuthenticationError):
            await c.get_json("/rest/api/2/project")


async def test_not_jira():
    def handler(request):
        return httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"})

    async with mock(handler) as c:
        with pytest.raises(UnexpectedResponseError) as ei:
            await c.test_connection()
    assert "text/html" in ei.value.message or "does not look like" in ei.value.message


async def test_redirect_to_sso_is_reported_not_followed():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://sso.example.test/saml/login"})

    async with mock(handler) as c:
        with pytest.raises(UnexpectedResponseError) as ei:
            await c.list_projects()
    assert "sso.example.test" in ei.value.message
    assert "SSO" in ei.value.hint


async def test_html_401_page_mentions_html():
    def handler(request):
        return httpx.Response(401, text="<html><head><title>Log in - Jira</title></head></html>",
                              headers={"content-type": "text/html"})

    async with mock(handler) as c:
        with pytest.raises(AuthenticationError) as ei:
            await c.myself()
    assert "HTML page" in ei.value.message and "Log in - Jira" in ei.value.message


# ---------------------------------------------------------- error mapping
@pytest.mark.parametrize(
    "status,exc",
    [(400, BadRequestError), (401, AuthenticationError), (403, PermissionDeniedError), (404, NotFoundError),
     (409, ConflictError), (429, RateLimitedError), (500, ServerError)],
)
async def test_error_mapping(status, exc):
    body = {"errorMessages": [f"boom {status}"], "errors": {"summary": "bad"}}
    async with mock(lambda r: httpx.Response(status, json=body), max_retries=0) as c:
        with pytest.raises(exc) as ei:
            await c.get_json("/rest/api/2/project")
    assert ei.value.status == status
    assert f"boom {status}" in ei.value.message
    assert ei.value.field_errors == {"summary": "bad"}


async def test_timeout_maps_to_timeout_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    async with mock(handler) as c:
        with pytest.raises(TimeoutError_):
            await c.list_projects()


async def test_tls_failure_maps_and_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed: unable to get local issuer certificate")
        except ssl.SSLError as e:
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]", request=request) from e

    async with mock(handler) as c:
        with pytest.raises(TLSError) as ei:
            await c.list_projects()
    assert len(calls) == 1
    assert "CA" in ei.value.hint


async def test_dns_failure_has_specific_message():
    def handler(request):
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known", request=request)

    async with mock(handler) as c:
        with pytest.raises(NetworkError) as ei:
            await c.list_projects()
    assert "DNS" in ei.value.message and "VPN" in ei.value.hint


async def test_connection_refused_is_network_error():
    def handler(request):
        raise httpx.ConnectError("[Errno 61] Connection refused", request=request)

    async with mock(handler) as c:
        with pytest.raises(NetworkError) as ei:
            await c.list_projects()
    assert "Could not connect" in ei.value.message


# ----------------------------------------------------------------- retries
async def test_get_retried_on_503_then_succeeds():
    responses = [httpx.Response(503), httpx.Response(502), httpx.Response(200, json=[])]
    calls = []

    def handler(request):
        calls.append(request)
        return responses[len(calls) - 1]

    async with mock(handler, max_retries=2) as c:
        assert await c.list_projects() == []
    assert len(calls) == 3


async def test_get_retries_are_bounded():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectTimeout("nope", request=request)

    async with mock(handler, max_retries=2) as c:
        with pytest.raises(TimeoutError_):
            await c.list_projects()
    assert len(calls) == 3


async def test_search_post_is_retried_but_create_is_not():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    async with mock(handler, max_retries=2) as c:
        with pytest.raises(ServerError):
            await c.search("project = X")
        assert len(calls) == 3  # search is idempotent
        calls.clear()
        with pytest.raises(ServerError):
            await c.create_issue({"summary": "x"})
        assert len(calls) == 1  # never retried
    assert calls[0].headers["x-atlassian-token"] == "no-check"


async def test_retry_after_is_honoured(monkeypatch):
    slept = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr("jiratui.api.client.asyncio.sleep", fake_sleep)
    responses = [httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(200, json=[])]
    async with mock(lambda r: responses.pop(0), max_retries=1) as c:
        await c.list_projects()
    assert slept == [3.0]


# ------------------------------------------------------------------ paging
def test_page_styles():
    agile = Page.from_json({"startAt": 0, "maxResults": 2, "isLast": False, "values": [{"a": 1}, {"a": 2}]}, dict)
    assert not agile.is_last and agile.next_start == 2
    search = Page.from_json({"startAt": 50, "maxResults": 50, "total": 120, "issues": [{}] * 50}, dict, key="issues")
    assert not search.is_last and search.next_start == 100 and search.total == 120
    last = Page.from_json({"startAt": 100, "maxResults": 50, "total": 120, "issues": [{}] * 20}, dict, key="issues")
    assert last.is_last and last.next_start is None
    plain_full = Page.from_json([{}] * 10, dict, start_at=0, max_results=10)
    assert not plain_full.is_last
    plain_short = Page.from_json([{}] * 3, dict, start_at=10, max_results=10)
    assert plain_short.is_last
    empty = Page.from_json({"isLast": False, "values": []}, dict)
    assert empty.is_last  # never loop on an empty non-last page


async def test_pager_stops_on_non_advancing_cursor():
    async def fetch(start, limit):
        # A server that ignores startAt.
        return Page(values=[1], start_at=0, max_results=1, is_last=False)

    pager = Pager(fetch)
    await pager.load_next()
    await pager.load_next()
    assert pager.exhausted


async def test_search_paging_with_pager(client, fake):
    cat = await client.fields()
    pager = Pager(lambda s, n: client.search("project = PLAT ORDER BY key ASC", s, n, catalog=cat), limit=50)
    first = await pager.load_next()
    assert len(first) == 50 and pager.total == 111 and not pager.exhausted
    await pager.load_next()
    await pager.load_next()
    assert pager.exhausted and len(pager.items) == 111
    assert [i.key for i in pager.items[:3]] == ["PLAT-1", "PLAT-2", "PLAT-3"]
    starts = [json.loads(r.content)["startAt"] for r in fake.requests if r.url.path.endswith("/search")]
    assert starts == [0, 50, 100]


async def test_iter_all_is_bounded(client):
    items = await client.collect(client.iter_all("/rest/agile/1.0/board/1/sprint", dict, limit=2, max_items=3))
    assert len(items) == 3


# ------------------------------------------------------------------ agile
async def test_boards_sprints_backlog(client, fake):
    boards = await client.all_boards("MOB")
    assert [(b.id, b.type) for b in boards] == [(2, "scrum"), (3, "kanban")]
    sprints = await client.board_sprints(1)
    assert [s.state for s in sprints] == ["active", "future", "future"]
    with_closed = await client.board_sprints(1, include_closed=True)
    assert with_closed[3].name == "PLAT Sprint 6"  # most recently closed first
    assert await client.board_estimation_field(1) == "customfield_10002"
    assert await client.board_estimation_field(4) == ""
    cat = await client.fields()
    backlog = await client.board_backlog(1, 'labels in ("tech-debt")', fields=cat.issue_fields(), catalog=cat)
    assert backlog.values and all("tech-debt" in i.labels for i in backlog.values)
    assert all(i.status.category_key != "done" for i in backlog.values)
    req = fake.requests[-1]
    assert req.url.path == "/rest/agile/1.0/board/1/backlog" and req.url.params["jql"] == 'labels in ("tech-debt")'
    with pytest.raises(BadRequestError):
        await client.board_backlog(4)  # kanban: no backlog endpoint


async def test_sprint_issues_agile_endpoint(client):
    cat = await client.fields()
    page = await client.sprint_issues(12, 'assignee = "alice"', catalog=cat)
    assert {i.key for i in page.values} == {"PLAT-3", "PLAT-13", "PLAT-19", "PLAT-25", "PLAT-31"}
    assert page.values[0].sprints[0].name == "PLAT Sprint 12"  # Agile API returns sprint objects


async def test_sprint_assignees_distinct(client):
    users = await client.sprint_assignees(12)
    names = [u.name for u in users]
    assert "alice" in names and len(names) == len(set(names))


def test_legacy_sprint_string_parsing():
    s = parse_sprint_value("com.atlassian.greenhopper.service.sprint.Sprint@1f[id=12,rapidViewId=1,state=ACTIVE,"
                           "name=Sprint 12, part 2,startDate=2026-01-01,endDate=<null>,sequence=12,goal=]")
    assert (s.id, s.state, s.name) == (12, "active", "Sprint 12, part 2")
    assert parse_sprint_value({"id": 3, "name": "X", "state": "closed"}).state == "closed"
    assert parse_sprint_value(None) is None


# ------------------------------------------------------------------ search
async def test_search_and_fields(client):
    cat = await client.fields()
    assert (cat.story_points_id, cat.sprint_id, cat.epic_link_id) == ("customfield_10002", "customfield_10001",
                                                                      "customfield_10003")
    page = await client.search("key = PLAT-3", fields=cat.issue_fields(), catalog=cat)
    issue = page.values[0]
    assert issue.story_points == 3 and issue.sprint_label == "PLAT Sprint 12" and issue.epic_link == "PLAT-1"
    assert issue.assignee.name == "alice" and issue.labels == ["backend", "ux/ui"]


async def test_search_jql_errors(client):
    with pytest.raises(JqlError) as ei:
        await client.search("project = NOPE AND foo = 1")
    assert ei.value.error_messages == [
        "The value 'NOPE' does not exist for the field 'project'.",
        "Field 'foo' does not exist or you do not have permission to view it.",
    ]
    with pytest.raises(JqlError) as ei:
        await client.search("project = PLAT AND (status = Done")
    assert "Error in the JQL Query" in ei.value.message
    assert await client.validate_jql("project = PLAT") == 111


async def test_search_sends_boolean_validate_query(client, fake):
    await client.search("project = PLAT")
    body = json.loads(next(r for r in fake.requests if r.url.path.endswith("/search")).content)
    assert body["validateQuery"] is True


async def test_fake_rejects_cloud_validate_query_like_dc(client):
    # Regression: DC 10.3 answers validateQuery="strict" with a text/plain 400 for every search.
    with pytest.raises(BadRequestError) as ei:
        await client.post_json("/rest/api/2/search", {"jql": "project = PLAT", "validateQuery": "strict"})
    assert "java.lang.Boolean" in ei.value.message


async def test_plain_text_error_body_is_shown():
    def handler(request):
        return httpx.Response(400, text="Cannot deserialize value\n at [Source: x]",
                              headers={"content-type": "text/plain;charset=UTF-8"})

    async with mock(handler) as c:
        with pytest.raises(JqlError) as ei:
            await c.search("project = X")
    assert ei.value.message == "Cannot deserialize value"


async def test_project_labels_and_suggestions(client):
    labels = await client.project_labels("PLAT")
    assert {"backend", "ux/ui", "release:2.0", "o'reilly"} <= set(labels)
    assert "android" not in labels  # MOB-only label
    sugg = await client.jql_suggestions("labels", "rel")
    assert [s.value for s in sugg] == ["release:2.0"]
    assert sugg[0].display == "release:2.0"  # <b> highlight tags stripped
    data = await client.jql_autocomplete()
    assert data.field_named("Story Points").cfid == "cf[10002]"
    assert "currentUser()" in data.functions


async def test_assignable_users_plain_array_paging(client, fake):
    users = await client.assignable_users(project_key="PLAT")
    assert [u.name for u in users] == ["alice", "bob", "carol", "dave.oneil", "mary ann", "j.smith@corp"]
    assert all(u.active for u in users)  # inactive users dropped
    with pytest.raises(ConfigError):
        await client.assignable_users()


async def test_favourite_filters_and_metadata(client):
    favs = await client.favourite_filters()
    assert [f.name for f in favs] == ["My open issues", "High priority bugs"]
    assert {t.name for t in await client.issue_types()} >= {"Story", "Bug", "Task", "Epic", "Sub-task"}
    assert [p.name for p in await client.priorities()][0] == "Highest"
    assert {s.name for s in await client.statuses("PLAT")} == {"To Do", "In Progress", "In Review", "Done"}


# ------------------------------------------------------------------ issues
async def test_get_issue_detail(client):
    cat = await client.fields()
    issue = await client.get_issue("PLAT-3", cat)
    assert [s.key for s in issue.subtasks] == ["PLAT-4"]
    assert issue.links[0].relation == "blocks" and issue.links[0].issue.key == "PLAT-6"
    assert len(issue.comments) == 2 and issue.comments[0].author.name == "carol"
    assert "h2. Problem" in issue.description
    blocked = await client.get_issue("PLAT-6", cat)
    assert blocked.links[0].relation == "is blocked by"
    with pytest.raises(NotFoundError):
        await client.get_issue("PLAT-9999")


async def test_transition_comment_assign(client, fake):
    ts = await client.transitions("PLAT-5")
    assert [(t.name, t.to_status) for t in ts] == [("Start Progress", "In Progress"), ("Done", "Done")]
    await client.transition("PLAT-5", "11")
    assert fake.issue("PLAT-5")["fields"]["status"]["name"] == "In Progress"
    await client.add_comment("PLAT-5", "hello *there*")
    assert fake.comments_posted[-1] == ("PLAT-5", "hello *there*")
    with pytest.raises(ConfigError):
        await client.add_comment("PLAT-5", "  ")
    await client.assign("PLAT-5", "mary ann")
    assert fake.issue("PLAT-5")["fields"]["assignee"]["name"] == "mary ann"
    with pytest.raises(BadRequestError) as ei:
        await client.assign("PLAT-5", "nobody-here")
    assert "cannot be assigned" in ei.value.field_errors["assignee"]
    await client.assign("PLAT-5", None)
    assert fake.issue("PLAT-5")["fields"]["assignee"] is None


# -------------------------------------------------------------- createmeta
async def test_createmeta_new_endpoints(client):
    types = await client.createmeta_issue_types("PLAT")
    assert client.createmeta_mode == "issuetypes"
    assert {t.name for t in types} == {"Epic", "Story", "Task", "Bug", "Sub-task"}
    fields = {f.field_id: f for f in await client.createmeta_fields("PLAT", "10004")}
    assert fields["summary"].needs_input and not fields["reporter"].needs_input
    assert fields["customfield_10005"].required and [a.label for a in fields["customfield_10005"].allowed_values] == \
        ["Red", "Blue", "Green"]


async def test_createmeta_falls_back_to_legacy():
    fake = FakeJira(new_createmeta=False)
    async with JiraClient(DEMO_URL, BearerTokenAuth(DEMO_TOKEN), transport=fake.transport()) as c:
        types = await c.createmeta_issue_types("PLAT")
        assert c.createmeta_mode == "legacy" and len(types) == 5
        fields = await c.createmeta_fields("PLAT", "10000")
    assert {f.field_id for f in fields} >= {"summary", "customfield_10004"}
    legacy_calls = [r for r in fake.requests if r.url.path == "/rest/api/2/issue/createmeta"]
    assert len(legacy_calls) == 1  # cached between the two calls


async def test_createmeta_old_jira_version_uses_legacy():
    fake = FakeJira(version="8.3.0")
    async with JiraClient(DEMO_URL, BasicAuth("alice", DEMO_PASSWORD), transport=fake.transport()) as c:
        await c.createmeta_issue_types("MOB")
        assert c.createmeta_mode == "legacy"


async def test_create_issue_and_field_errors(client, fake):
    with pytest.raises(BadRequestError) as ei:
        await client.create_issue({"project": {"key": "PLAT"}, "issuetype": {"id": "10004"},
                                   "labels": ["has space"]})
    errs = ei.value.field_errors
    assert errs["summary"] == "You must specify a summary of the issue."
    assert errs["priority"] == "Priority is required." and errs["customfield_10005"] == "Team is required."
    assert "contains spaces" in errs["labels"]
    key = await client.create_issue({
        "project": {"key": "PLAT"}, "issuetype": {"id": "10001"}, "summary": "From test",
        "customfield_10002": 5, "customfield_10001": 13, "customfield_10003": "PLAT-2", "labels": ["new"],
        "assignee": {"name": "bob"},
    })
    assert key == "PLAT-112"
    cat = await client.fields()
    issue = await client.get_issue(key, cat)
    assert (issue.summary, issue.story_points, issue.epic_link, issue.sprint_label) == \
        ("From test", 5, "PLAT-2", "PLAT Sprint 13")


def test_issue_parsing_is_lenient():
    issue = Issue.from_json({"key": "X-1", "fields": {"priority": None, "assignee": None, "labels": None,
                                                      "customfield_10002": "n/a"}}, story_points_id="customfield_10002")
    assert issue.key == "X-1" and issue.assignee is None and issue.labels == [] and issue.story_points is None
