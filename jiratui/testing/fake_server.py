"""In-memory fake of the Jira Server / Data Center REST endpoints used by jiratui.

Used by the unit/TUI tests and by ``jiratui --demo``. It is an
``httpx.MockTransport`` handler, so no sockets are opened.

Shapes follow the Jira Server REST API 2 and Agile 1.0 documentation:
``startAt``/``maxResults``/``total`` searches, ``isLast`` Agile lists, plain
array endpoints, ``{"errorMessages": [], "errors": {}}`` error bodies, the
legacy sprint-field string format in the core API, both createmeta styles,
and Jira's header-based auth failures (``X-Seraph-LoginReason``,
``X-Authentication-Denied-Reason``).
"""

from __future__ import annotations

import base64
import copy
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote

import httpx

from . import fake_jql
from .fake_jql import JqlSyntaxError

DEMO_URL = "https://jira.example.test"
DEMO_TOKEN = "demo-pat-0123456789abcdef"
DEMO_USER = "alice"
DEMO_PASSWORD = "s3cret-pass"

J = Dict[str, Any]

SP = "customfield_10002"
SPRINT = "customfield_10001"
EPIC_LINK = "customfield_10003"
EPIC_NAME = "customfield_10004"
TEAM = "customfield_10005"


def _err(status: int, *messages: str, errors: Optional[Dict[str, str]] = None,
         headers: Optional[Dict[str, str]] = None) -> httpx.Response:
    return httpx.Response(status, json={"errorMessages": list(messages), "errors": errors or {}},
                          headers=headers or {})


def _user(name: str, display: str) -> J:
    return {
        "self": f"{DEMO_URL}/rest/api/2/user?username={name}",
        "name": name,
        "key": "JIRAUSER" + str(sum(map(ord, name)) % 100000),
        "displayName": display,
        "emailAddress": f"{re.sub(r'[^a-z0-9.]', '', name.lower())}@example.test",
        "active": True,
        "timeZone": "UTC",
    }


USERS: Dict[str, J] = {u["name"]: u for u in (
    _user("alice", "Alice Anders"),
    _user("bob", "Bob Brown"),
    _user("carol", "Carol Chen"),
    _user("dave.oneil", "Dave O'Neil"),
    _user("mary ann", "Mary Ann Smith"),  # usernames with spaces exist on Server/DC
    _user("j.smith@corp", 'John "JJ" Smith'),
    _user("inactive.ian", "Ian (inactive)"),
)}
USERS["inactive.ian"]["active"] = False

ISSUE_TYPES = [
    {"id": "10000", "name": "Epic", "subtask": False, "description": "A big user story"},
    {"id": "10001", "name": "Story", "subtask": False, "description": "A user story"},
    {"id": "10002", "name": "Task", "subtask": False, "description": "A task"},
    {"id": "10004", "name": "Bug", "subtask": False, "description": "A problem"},
    {"id": "10003", "name": "Sub-task", "subtask": True, "description": "A sub-task"},
]
STATUSES = [
    {"id": "1", "name": "To Do", "statusCategory": {"id": 2, "key": "new", "name": "To Do"}},
    {"id": "3", "name": "In Progress", "statusCategory": {"id": 4, "key": "indeterminate", "name": "In Progress"}},
    {"id": "10100", "name": "In Review", "statusCategory": {"id": 4, "key": "indeterminate", "name": "In Progress"}},
    {"id": "10001", "name": "Done", "statusCategory": {"id": 3, "key": "done", "name": "Done"}},
]
PRIORITIES = [{"id": str(i + 1), "name": n} for i, n in enumerate(["Highest", "High", "Medium", "Low", "Lowest"])]
TEAMS = [{"id": "20001", "value": "Red"}, {"id": "20002", "value": "Blue"}, {"id": "20003", "value": "Green"}]
# status name -> [(transition id, name, target status name)]
WORKFLOW = {
    "To Do": [("11", "Start Progress", "In Progress"), ("41", "Done", "Done")],
    "In Progress": [("21", "Send to Review", "In Review"), ("31", "Stop Progress", "To Do")],
    "In Review": [("41", "Done", "Done"), ("51", "Back to In Progress", "In Progress")],
    "Done": [("61", "Reopen", "To Do")],
}
LABEL_POOL = ["backend", "frontend", "ux/ui", "tech-debt", "release:2.0", "o'reilly", "security", "perf"]


def _schema(type_: str, *, system: str = "", custom: str = "", items: str = "", cid: int = 0) -> J:
    s: J = {"type": type_}
    if items:
        s["items"] = items
    if system:
        s["system"] = system
    if custom:
        s["custom"] = custom
        s["customId"] = cid
    return s


FIELDS: List[J] = [
    {"id": "summary", "name": "Summary", "custom": False, "schema": _schema("string", system="summary")},
    {"id": "description", "name": "Description", "custom": False, "schema": _schema("string", system="description")},
    {"id": "issuetype", "name": "Issue Type", "custom": False, "schema": _schema("issuetype", system="issuetype")},
    {"id": "project", "name": "Project", "custom": False, "schema": _schema("project", system="project")},
    {"id": "status", "name": "Status", "custom": False, "schema": _schema("status", system="status")},
    {"id": "assignee", "name": "Assignee", "custom": False, "schema": _schema("user", system="assignee")},
    {"id": "reporter", "name": "Reporter", "custom": False, "schema": _schema("user", system="reporter")},
    {"id": "priority", "name": "Priority", "custom": False, "schema": _schema("priority", system="priority")},
    {"id": "labels", "name": "Labels", "custom": False, "schema": _schema("array", items="string", system="labels")},
    {"id": "components", "name": "Component/s", "custom": False,
     "schema": _schema("array", items="component", system="components")},
    {"id": "fixVersions", "name": "Fix Version/s", "custom": False,
     "schema": _schema("array", items="version", system="fixVersions")},
    {"id": "resolution", "name": "Resolution", "custom": False, "schema": _schema("resolution", system="resolution")},
    {"id": "created", "name": "Created", "custom": False, "schema": _schema("datetime", system="created")},
    {"id": "updated", "name": "Updated", "custom": False, "schema": _schema("datetime", system="updated")},
    {"id": "duedate", "name": "Due Date", "custom": False, "schema": _schema("date", system="duedate")},
    {"id": "parent", "name": "Parent", "custom": False, "schema": _schema("issuelink", system="parent")},
    {"id": SPRINT, "name": "Sprint", "custom": True,
     "schema": _schema("array", items="string", custom="com.pyxis.greenhopper.jira:gh-sprint", cid=10001)},
    {"id": SP, "name": "Story Points", "custom": True,
     "schema": _schema("number", custom="com.atlassian.jira.plugin.system.customfieldtypes:float", cid=10002)},
    {"id": EPIC_LINK, "name": "Epic Link", "custom": True,
     "schema": _schema("any", custom="com.pyxis.greenhopper.jira:gh-epic-link", cid=10003)},
    {"id": EPIC_NAME, "name": "Epic Name", "custom": True,
     "schema": _schema("string", custom="com.pyxis.greenhopper.jira:gh-epic-label", cid=10004)},
    {"id": TEAM, "name": "Team", "custom": True,
     "schema": _schema("option", custom="com.atlassian.jira.plugin.system.customfieldtypes:select", cid=10005)},
]
for _f in FIELDS:
    _f.setdefault("navigable", True)
    _f["orderable"] = _f["id"] not in ("issuetype", "project", "status")
    _f["searchable"] = True
    _f["clauseNames"] = [_f["id"]] if not _f["custom"] else [f"cf[{_f['schema']['customId']}]", _f["name"]]

DESCRIPTION_MARKUP = """h2. Problem
Users see a *500 error* when logging in with an _expired_ session.

h3. Steps
# Open the login page
# Enter valid credentials
## (with an expired cookie)
# Press *Login*

* Affects {{/auth/session}}
* See [runbook|https://wiki.example.test/runbook] and ask [~carol]
{code:python}
def login(user):
    raise ServerError(500)
{code}
||Env||Result||
|staging|fails|
|prod|fails|
bq. Customer quote: it -used to- work."""


PULL_REQUESTS: Dict[str, List[J]] = {
    "PLAT-3": [
        {"id": "#42", "name": "Fix session expiry handling", "url": "https://bitbucket.example.com/projects/PLAT/repos/web/pull-requests/42",
         "status": "OPEN", "author": {"name": "Alice Anders"},
         "source": {"branch": "bugfix/PLAT-3-session"}, "destination": {"branch": "main"}},
        {"id": "#40", "name": "Add session logging", "url": "https://bitbucket.example.com/projects/PLAT/repos/web/pull-requests/40",
         "status": "MERGED", "author": {"name": "Bob Brown"},
         "source": {"branch": "feature/PLAT-3-logging"}, "destination": {"branch": "main"}},
    ],
}


class FakeJira(fake_jql.Context):
    def __init__(
        self,
        base_url: str = DEMO_URL,
        token: str = DEMO_TOKEN,
        username: str = DEMO_USER,
        password: str = DEMO_PASSWORD,
        version: str = "9.12.2",
        deployment_type: str = "Server",
        new_createmeta: bool = True,
        legacy_createmeta: bool = True,
        n_bulk_issues: int = 100,
        n_closed_sprints: int = 6,
        captcha_after: int = 0,
        anonymous_server_info: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.username = username
        self.password = password
        self.version = version
        self.deployment_type = deployment_type
        self.new_createmeta = new_createmeta
        self.legacy_createmeta = legacy_createmeta
        self.captcha_after = captcha_after
        self.anonymous_server_info = anonymous_server_info
        self.failed_logins = 0
        self.requests: List[httpx.Request] = []
        self.fail_next: List[Tuple[str, int]] = []  # (path-regex, status) one-shot failures
        self.created: List[J] = []
        self.comments_posted: List[Tuple[str, str]] = []
        self.searches: List[str] = []
        self._current_user = username
        self._build(n_bulk_issues, n_closed_sprints)

    # ----------------------------------------------------------- fixtures
    @property
    def version_tuple(self) -> Tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", self.version)[:3])

    def _build(self, n_bulk: int, n_closed: int) -> None:
        self.projects: List[J] = [
            {"id": "10000", "key": "PLAT", "name": "Platform", "projectTypeKey": "software"},
            {"id": "10001", "key": "MOB", "name": "Mobile Apps", "projectTypeKey": "software"},
            {"id": "10002", "key": "OPS", "name": "Operations", "projectTypeKey": "software"},
            {"id": "10003", "key": "LEG", "name": "Legacy (no board)", "projectTypeKey": "business"},
        ]
        for p in self.projects:
            p["self"] = f"{self.base_url}/rest/api/2/project/{p['id']}"
        self.components = {"PLAT": ["API", "Web", "Infra"], "MOB": ["iOS", "Android"], "OPS": ["On-call"], "LEG": []}
        self.assignable = {
            "PLAT": ["alice", "bob", "carol", "dave.oneil", "mary ann", "j.smith@corp", "inactive.ian"],
            "MOB": ["alice", "bob", "mary ann"],
            "OPS": ["carol", "dave.oneil"],
            "LEG": ["alice"],
        }
        self.boards: List[J] = [
            {"id": 1, "name": "PLAT Scrum", "type": "scrum", "project": "PLAT"},
            {"id": 2, "name": "MOB Scrum", "type": "scrum", "project": "MOB"},
            {"id": 3, "name": "MOB Kanban", "type": "kanban", "project": "MOB"},
            {"id": 4, "name": "OPS Kanban", "type": "kanban", "project": "OPS"},
        ]
        self.sprints: List[J] = []

        def sprint(sid: int, name: str, state: str, board: int, start: str = "", end: str = "",
                   complete: str = "", goal: str = "") -> J:
            s = {"id": sid, "name": name, "state": state, "originBoardId": board, "goal": goal,
                 "self": f"{self.base_url}/rest/agile/1.0/sprint/{sid}"}
            if start:
                s["startDate"] = start
            if end:
                s["endDate"] = end
            if complete:
                s["completeDate"] = complete
            self.sprints.append(s)
            return s

        for k in range(n_closed):
            sprint(100 + k, f"PLAT Sprint {k + 1}", "closed", 1, f"2025-{(k % 12) + 1:02d}-01T09:00:00.000Z",
                   f"2025-{(k % 12) + 1:02d}-14T17:00:00.000Z", f"2025-{(k % 12) + 1:02d}-14T17:05:00.000Z")
        sprint(12, "PLAT Sprint 12", "active", 1, "2026-11-10T09:00:00.000Z", "2026-11-24T17:00:00.000Z",
               goal="Ship the login fix")
        sprint(13, "PLAT Sprint 13", "future", 1, goal="Observability")
        sprint(14, "PLAT Sprint 14", "future", 1)
        sprint(20, "MOB Sprint 3", "closed", 2, "2026-10-01T09:00:00.000Z", "2026-10-14T17:00:00.000Z",
               "2026-10-14T17:00:00.000Z")
        sprint(21, "MOB Sprint 4", "active", 2, "2026-11-03T09:00:00.000Z", "2026-11-17T17:00:00.000Z")
        sprint(22, "MOB Sprint 5", "future", 2)

        self.issues: List[J] = []
        self._rank = 0

        def add(project: str, summary: str, *, itype: str = "Story", status: str = "To Do",
                assignee: Optional[str] = None, reporter: str = "bob", priority: str = "Medium",
                labels: Sequence[str] = (), sp: Optional[float] = None, sprints: Sequence[int] = (),
                epic: str = "", description: str = "", components: Sequence[str] = (),
                parent: str = "", comments: Sequence[Tuple[str, str]] = (), links: Sequence[Tuple[str, str]] = (),
                epic_name: str = "", day: int = 1) -> J:
            n = 1 + sum(1 for i in self.issues if i["fields"]["project"]["key"] == project)
            issue = self._make_issue(project, n, summary, itype=itype, status=status, assignee=assignee,
                                     reporter=reporter, priority=priority, labels=labels, sp=sp,
                                     epic=epic, description=description, components=components,
                                     parent=parent, epic_name=epic_name, day=day)
            issue["_sprints"] = list(sprints)
            issue["_comments"] = [{"id": str(9000 + len(self.issues) * 10 + i), "author": USERS[a], "body": b,
                                   "created": f"2026-11-{10 + i:02d}T10:00:00.000+0000"}
                                  for i, (a, b) in enumerate(comments)]
            issue["_links"] = list(links)
            self.issues.append(issue)
            return issue

        # --- PLAT: hand-crafted issues (tests rely on these) ---
        add("PLAT", "Authentication revamp", itype="Epic", epic_name="Auth", status="In Progress", assignee="alice")
        add("PLAT", "Observability", itype="Epic", epic_name="O11y", assignee="carol")
        add("PLAT", "Login page throws 500 on expired session", itype="Bug", status="In Progress",
            assignee="alice", priority="High", labels=["backend", "ux/ui"], sp=3, sprints=[12], epic="PLAT-1",
            description=DESCRIPTION_MARKUP, components=["API", "Web"],
            comments=[("carol", "Reproduced on *staging*."), ("alice", "Fix in review: see {{PR-42}}")],
            links=[("blocks", "PLAT-6")])
        add("PLAT", "Write regression test for session expiry", itype="Sub-task", parent="PLAT-3",
            assignee="bob", sprints=[12], status="To Do", sp=None)
        add("PLAT", "Add MFA enrolment screen", itype="Story", status="To Do", assignee=None, priority="Highest",
            labels=["frontend", "security"], sp=8, sprints=[12], epic="PLAT-1")
        add("PLAT", "Rotate signing keys", itype="Task", status="In Review", assignee="mary ann",
            priority="High", labels=["security", "release:2.0"], sp=2, sprints=[12], epic="PLAT-1")
        add("PLAT", "Dashboard latency regression", itype="Bug", status="To Do", assignee="j.smith@corp",
            priority="Medium", labels=["perf", "o'reilly"], sp=5, sprints=[12], epic="PLAT-2")
        add("PLAT", "Clean up legacy session code", itype="Task", status="Done", assignee="dave.oneil",
            priority="Low", labels=["tech-debt"], sp=1, sprints=[12])
        add("PLAT", "Metrics exporter", itype="Story", status="To Do", assignee="carol", sp=5, sprints=[13],
            epic="PLAT-2", labels=["backend"])
        add("PLAT", "Tracing for API gateway", itype="Story", status="To Do", assignee=None, sp=3, sprints=[13],
            epic="PLAT-2", labels=["backend", "perf"])
        add("PLAT", "Old flaky test", itype="Bug", status="To Do", assignee="bob", priority="Low",
            labels=["tech-debt"], sprints=[100 + n_closed - 1] if n_closed else [])
        # --- PLAT: bulk, deterministic ---
        types = ["Story", "Task", "Bug", "Story", "Task"]
        status_cycle = ["To Do", "To Do", "In Progress", "Done", "In Review"]
        people = [None, "alice", "bob", "carol", "dave.oneil", "mary ann"]
        prios = ["Medium", "High", "Low", "Medium", "Highest", "Lowest"]
        for i in range(n_bulk):
            lbls = [LABEL_POOL[i % len(LABEL_POOL)]] + ([LABEL_POOL[(i * 3) % len(LABEL_POOL)]] if i % 4 == 0 else [])
            if i < 20:
                spr: List[int] = [12]
            elif i < 26:
                spr = [13]
            elif i < 28:
                spr = [14]
            elif i < 36 and n_closed:
                spr = [100 + (i % n_closed)]
            else:
                spr = []
            st = status_cycle[i % len(status_cycle)]
            if spr and spr[0] >= 100:
                st = "Done"
            add("PLAT", f"Generated {types[i % len(types)].lower()} #{i:03d}", itype=types[i % len(types)],
                status=st, assignee=people[i % len(people)], priority=prios[i % len(prios)],
                labels=sorted(set(lbls)), sp=float((i % 5) + 1) if i % 3 else None, sprints=spr,
                day=(i % 28) + 1)
        # --- MOB ---
        add("MOB", "Push notifications on Android 15", itype="Bug", status="In Progress", assignee="mary ann",
            priority="High", labels=["android"], sp=3, sprints=[21], components=["Android"])
        add("MOB", "Dark mode", itype="Story", status="To Do", assignee="alice", labels=["ios", "ux/ui"], sp=5,
            sprints=[21], components=["iOS"])
        add("MOB", "Offline cache", itype="Story", status="To Do", assignee=None, labels=["perf"], sp=8,
            sprints=[22])
        add("MOB", "App store screenshots", itype="Task", status="To Do", assignee="bob", labels=["release:2.0"])
        add("MOB", "Crash on launch (iPad)", itype="Bug", status="Done", assignee="alice", priority="Highest",
            labels=["ios"], sprints=[20])
        # --- OPS (kanban) ---
        add("OPS", "Renew TLS certificates", itype="Task", status="To Do", assignee="carol", labels=["security"])
        add("OPS", "Disk alerts on db-3", itype="Bug", status="In Progress", assignee="dave.oneil",
            priority="Highest", labels=["on-call"])
        add("OPS", "Runbook for failover", itype="Task", status="Done", assignee="carol")
        # --- LEG (no board) ---
        add("LEG", "Archive old reports", itype="Task", status="To Do", assignee="alice", labels=["tech-debt"])

        self.filters = [
            {"id": "10100", "name": "My open issues",
             "jql": "assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC",
             "owner": USERS["alice"], "favourite": True},
            {"id": "10101", "name": "High priority bugs",
             "jql": "issuetype = Bug AND priority in (Highest, High)", "owner": USERS["bob"], "favourite": True},
        ]

    def _make_issue(self, project: str, n: int, summary: str, **kw: Any) -> J:
        key = f"{project}-{n}"
        proj = next(p for p in self.projects if p["key"] == project)
        self._rank += 1
        st = next(s for s in STATUSES if s["name"] == kw.get("status", "To Do"))
        itype = next(t for t in ISSUE_TYPES if t["name"] == kw.get("itype", "Story"))
        prio = next(p for p in PRIORITIES if p["name"] == kw.get("priority", "Medium"))
        day = int(kw.get("day", 1))
        fields: J = {
            "summary": summary,
            "description": kw.get("description") or None,
            "issuetype": dict(itype),
            "project": {"id": proj["id"], "key": proj["key"], "name": proj["name"]},
            "status": dict(st),
            "assignee": USERS[kw["assignee"]] if kw.get("assignee") else None,
            "reporter": USERS[kw.get("reporter") or "bob"],
            "priority": dict(prio),
            "labels": list(kw.get("labels") or []),
            "components": [{"name": c} for c in kw.get("components") or []],
            "fixVersions": [],
            "resolution": {"name": "Done"} if st["name"] == "Done" else None,
            "created": f"2026-10-{day:02d}T09:00:00.000+0000",
            "updated": f"2026-11-{min(day, 17):02d}T12:00:00.000+0000",
            SP: kw.get("sp"),
            EPIC_LINK: kw.get("epic") or None,
            EPIC_NAME: kw.get("epic_name") or None,
            TEAM: None,
        }
        if kw.get("parent"):
            fields["parent"] = {"key": kw["parent"]}
        return {"id": str(20000 + len(self.issues)), "key": key, "_rank": self._rank,
                "self": f"{self.base_url}/rest/api/2/issue/{20000 + len(self.issues)}", "fields": fields}

    # -------------------------------------------------------------- utils
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def issue(self, key: str) -> Optional[J]:
        return next((i for i in self.issues if i["key"].lower() == key.lower()), None)

    def sprint_by_id(self, sid: int) -> Optional[J]:
        return next((s for s in self.sprints if s["id"] == sid), None)

    def _authenticate(self, request: httpx.Request) -> Tuple[Optional[str], Optional[httpx.Response]]:
        """``(username, None)`` on success, ``(None, error_response)`` on bad credentials,
        ``(None, None)`` for anonymous requests."""
        h = request.headers.get("authorization", "")
        if not h:
            return None, None
        if h.startswith("Bearer "):
            if self.version_tuple[:2] < (8, 14):
                return None, None  # PATs unknown before 8.14: the header is ignored (anonymous)
            if h[7:] == self.token:
                return self.username, None
            return None, _err(401, "The personal access token is invalid or expired.")
        if h.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
            except Exception:
                return None, _err(401, "Malformed Basic credentials")
            if self.captcha_after and self.failed_logins >= self.captcha_after:
                return None, httpx.Response(403, text="<html><title>Forbidden</title></html>", headers={
                    "content-type": "text/html;charset=UTF-8",
                    "X-Seraph-LoginReason": "AUTHENTICATION_DENIED",
                    "X-Authentication-Denied-Reason": f"CAPTCHA_CHALLENGE; login-url={self.base_url}/login.jsp",
                })
            if user == self.username and pw in (self.password, self.token):
                self.failed_logins = 0
                return user, None
            self.failed_logins += 1
            return None, httpx.Response(401, text="<html><title>Unauthorized (401)</title></html>", headers={
                "content-type": "text/html;charset=UTF-8", "X-Seraph-LoginReason": "AUTHENTICATED_FAILED"})
        return None, _err(401, "Unsupported authorization scheme")

    @staticmethod
    def _int(q: Any, name: str, default: int) -> int:
        try:
            return int(q.get(name, default))
        except (TypeError, ValueError):
            return default

    def _paged(self, values: List[Any], start: int, limit: int, *, key: str = "values",
               cap: int = 50, total: bool = False, is_last: bool = True) -> J:
        limit = max(0, min(limit, cap))
        chunk = values[start: start + limit]
        out: J = {"startAt": start, "maxResults": limit, key: chunk}
        if is_last:
            out["isLast"] = start + len(chunk) >= len(values)
        if total:
            out["total"] = len(values)
        return out

    # ------------------------------------------------------- JQL context
    def field_kind(self, field: str) -> Optional[str]:
        f = self._alias(field)
        if f in ("summary", "description", "text", "comment"):
            return "text"
        if f in ("sp",):
            return "number"
        if f in ("created", "updated", "duedate"):
            return "date"
        if f in ("project", "key", "labels", "assignee", "reporter", "status", "statuscategory", "issuetype",
                 "priority", "sprint", "epic", "component", "resolution", "fixversion", "team", "parent", "rank"):
            return "list"
        return None

    @staticmethod
    def _alias(field: str) -> str:
        f = field.lower()
        return {
            "type": "issuetype", "issuekey": "key", "id": "key", "cf[10001]": "sprint",
            "story points": "sp", "cf[10002]": "sp", "epic link": "epic", "cf[10003]": "epic",
            "components": "component", "fixversions": "fixversion", "cf[10005]": "team",
            "statuscategory": "statuscategory", "category": "statuscategory",
        }.get(f, f)

    def values(self, issue: J, field: str) -> List[str]:
        f = self._alias(field)
        fl = issue["fields"]
        if f == "project":
            return [fl["project"]["key"], fl["project"]["name"], fl["project"]["id"]]
        if f == "key":
            return [issue["key"], issue["id"]]
        if f in ("summary", "description"):
            return [fl.get(f) or ""] if fl.get(f) else []
        if f == "text":
            return [fl["summary"], fl.get("description") or ""] + [c["body"] for c in issue.get("_comments", [])]
        if f == "comment":
            return [c["body"] for c in issue.get("_comments", [])]
        if f == "labels":
            return list(fl.get("labels") or [])
        if f in ("assignee", "reporter"):
            u = fl.get(f)
            return [u["name"], u["key"]] if u else []
        if f == "status":
            return [fl["status"]["name"], fl["status"]["id"]]
        if f == "statuscategory":
            c = fl["status"]["statusCategory"]
            return [c["key"], c["name"], str(c["id"])]
        if f == "issuetype":
            return [fl["issuetype"]["name"], fl["issuetype"]["id"]]
        if f == "priority":
            return [fl["priority"]["name"], fl["priority"]["id"]] if fl.get("priority") else []
        if f == "sprint":
            out: List[str] = []
            for sid in issue.get("_sprints", []):
                s = self.sprint_by_id(sid)
                out += [str(sid)] + ([s["name"]] if s else [])
            return out
        if f == "sp":
            return [str(fl[SP])] if fl.get(SP) is not None else []
        if f == "epic":
            return [fl[EPIC_LINK]] if fl.get(EPIC_LINK) else []
        if f == "component":
            return [c["name"] for c in fl.get("components") or []]
        if f == "resolution":
            return [fl["resolution"]["name"]] if fl.get("resolution") else []
        if f == "fixversion":
            return [v["name"] for v in fl.get("fixVersions") or []]
        if f == "team":
            t = fl.get(TEAM)
            return [t["value"], t["id"]] if t else []
        if f == "parent":
            return [fl["parent"]["key"]] if fl.get("parent") else []
        if f in ("created", "updated", "duedate"):
            return [str(fl.get(f) or "")[:16].replace("T", " ")] if fl.get(f) else []
        return []

    def function(self, name: str, field: str) -> Optional[List[str]]:
        if name == "currentuser":
            return [self._current_user]
        if name in ("opensprints", "closedsprints", "futuresprints"):
            states = {"opensprints": ("active", "future"), "closedsprints": ("closed",),
                      "futuresprints": ("future",)}[name]
            return [str(s["id"]) for s in self.sprints if s["state"] in states]
        if name in ("now", "startofday"):
            return ["2026-11-17 00:00"]
        return None

    def validate_value(self, field: str, value: str) -> Optional[str]:
        f = self._alias(field)
        v = value.lower()
        known: Optional[List[str]] = None
        if f == "project":
            known = [x for p in self.projects for x in (p["key"], p["name"], p["id"])]
        elif f == "issuetype":
            known = [x for t in ISSUE_TYPES for x in (t["name"], t["id"])] + ["standardissuetypes()"]
        elif f == "status":
            known = [x for s in STATUSES for x in (s["name"], s["id"])]
        elif f == "priority":
            known = [x for p in PRIORITIES for x in (p["name"], p["id"])]
        elif f in ("assignee", "reporter"):
            known = [x for u in USERS.values() for x in (u["name"], u["key"])]
        elif f == "resolution":
            known = ["unresolved", "done", "won't do", "duplicate"]
        elif f == "statuscategory":
            known = ["new", "to do", "indeterminate", "in progress", "done", "2", "3", "4"]
        if known is not None and v not in (k.lower() for k in known):
            name = {"issuetype": "issuetype", "statuscategory": "statusCategory"}.get(f, field)
            return f"The value '{value}' does not exist for the field '{name}'."
        return None

    def sort_key(self, issue: J, field: str) -> Any:
        f = self._alias(field)
        if f == "rank":
            return issue["_rank"]
        if f == "key":
            p, _, n = issue["key"].partition("-")
            return (p, int(n))
        if f == "priority":
            return int(issue["fields"]["priority"]["id"]) if issue["fields"].get("priority") else 99
        if f == "sp":
            v = issue["fields"].get(SP)
            return (v is None, v or 0)
        vals = self.values(issue, f)
        return (not vals, vals[0].lower() if vals else "")

    def _run_jql(self, jql: str, pool: Optional[List[J]] = None) -> List[J]:
        """Filter issues; ``resolution = Unresolved`` handled like Jira."""
        fixed = re.sub(r"(?i)\bresolution\s*=\s*unresolved\b", "resolution is EMPTY", jql or "")
        fixed = re.sub(r"(?i)\bresolution\s*!=\s*unresolved\b", "resolution is not EMPTY", fixed)
        return fake_jql.run(fixed, list(self.issues if pool is None else pool), self)

    # --------------------------------------------------------- rendering
    def _sprint_string(self, s: J) -> str:
        return (f"com.atlassian.greenhopper.service.sprint.Sprint@{s['id']:x}[id={s['id']},"
                f"rapidViewId={s['originBoardId']},state={s['state'].upper()},name={s['name']},"
                f"startDate={s.get('startDate', '<null>')},endDate={s.get('endDate', '<null>')},"
                f"completeDate={s.get('completeDate', '<null>')},sequence={s['id']},goal={s.get('goal') or ''}]")

    def _render(self, issue: J, fields_param: Optional[Sequence[str]] = None, agile: bool = False) -> J:
        f = copy.deepcopy(issue["fields"])
        sprints = [s for s in (self.sprint_by_id(x) for x in issue.get("_sprints", [])) if s]
        if agile:
            f[SPRINT] = [copy.deepcopy(s) for s in sprints] or None
            current = [s for s in sprints if s["state"] != "closed"]
            f["sprint"] = copy.deepcopy(current[0]) if current else None
            f["closedSprints"] = [copy.deepcopy(s) for s in sprints if s["state"] == "closed"]
        else:
            f[SPRINT] = [self._sprint_string(s) for s in sprints] or None
        f["comment"] = {"comments": copy.deepcopy(issue.get("_comments", [])),
                        "total": len(issue.get("_comments", [])), "maxResults": 50, "startAt": 0}
        f["subtasks"] = [self._ref(i) for i in self.issues if (i["fields"].get("parent") or {}).get("key") == issue["key"]]
        if f.get("parent"):
            p = self.issue(f["parent"]["key"])
            f["parent"] = self._ref(p) if p else f["parent"]
        links = []
        for rel, other_key in issue.get("_links", []):
            other = self.issue(other_key)
            if other:
                links.append({"id": "1", "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                              "outwardIssue": self._ref(other)})
        for other in self.issues:
            for rel, k in other.get("_links", []):
                if k == issue["key"]:
                    links.append({"id": "2", "type": {"name": "Blocks", "inward": "is blocked by",
                                                      "outward": "blocks"}, "inwardIssue": self._ref(other)})
        f["issuelinks"] = links
        wanted = [x for x in (fields_param or []) if x]
        if wanted and not any(w in ("*all", "*navigable") for w in wanted):
            f = {k: v for k, v in f.items() if k in wanted}
        return {"id": issue["id"], "key": issue["key"], "self": issue["self"], "fields": f}

    @staticmethod
    def _ref(i: J) -> J:
        return {"id": i["id"], "key": i["key"], "fields": {
            "summary": i["fields"]["summary"], "status": i["fields"]["status"],
            "issuetype": i["fields"]["issuetype"], "priority": i["fields"]["priority"]}}

    # ------------------------------------------------------------ handler
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = unquote(request.url.path)
        if self.base_url.count("/") > 2:  # context path, e.g. https://host/jira
            ctx = "/" + self.base_url.split("/", 3)[3]
            if not path.startswith(ctx + "/"):
                return httpx.Response(404, text="<html><title>Not found</title></html>",
                                      headers={"content-type": "text/html"})
            path = path[len(ctx):]
        for i, (pat, status) in enumerate(self.fail_next):
            if re.search(pat, path):
                del self.fail_next[i]
                return _err(status, f"Injected failure {status}")
        if not (path.startswith(("/rest/api/2/", "/rest/agile/1.0/", "/rest/dev-status/"))):
            return httpx.Response(404, text="<html><title>Oops, you've found a dead link.</title></html>",
                                  headers={"content-type": "text/html"})

        user, failure = self._authenticate(request)
        if path == "/rest/api/2/serverInfo" and (self.anonymous_server_info or user):
            resp = httpx.Response(200, json={
                "baseUrl": self.base_url, "version": self.version,
                "versionNumbers": list(self.version_tuple), "deploymentType": self.deployment_type,
                "buildNumber": 912002, "buildDate": "2026-01-01T00:00:00.000+0000",
                "scmInfo": "abc", "serverTitle": "Jira (demo)"})
            if failure is not None and "x-seraph-loginreason" in failure.headers:
                resp.headers["X-Seraph-LoginReason"] = failure.headers["x-seraph-loginreason"]
            resp.headers["X-AUSERNAME"] = user or "anonymous"
            return resp
        if failure is not None:
            return failure
        if user is None:
            return _err(401, "You are not authenticated. Authentication required to perform this operation.",
                        headers={"X-AUSERNAME": "anonymous"})
        self._current_user = user
        try:
            resp = self._route(request, path)
        except JqlSyntaxError as exc:
            resp = _err(400, *exc.messages)
        resp.headers["X-AUSERNAME"] = user
        return resp

    def _body(self, request: httpx.Request) -> J:
        try:
            return json.loads(request.content or b"{}")
        except ValueError:
            return {}

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
        m = request.method
        q = request.url.params
        api = "/rest/api/2"
        agile = "/rest/agile/1.0"

        if path == f"{api}/myself" and m == "GET":
            return httpx.Response(200, json=USERS[self._current_user])
        if path == f"{api}/project" and m == "GET":
            return httpx.Response(200, json=self.projects)
        mm = re.match(rf"^{api}/project/([^/]+)/statuses$", path)
        if mm and m == "GET":
            if not self._project(mm.group(1)):
                return _err(404, f"No project could be found with key '{mm.group(1)}'.")
            return httpx.Response(200, json=[{"id": t["id"], "name": t["name"], "subtask": t["subtask"],
                                              "statuses": STATUSES} for t in ISSUE_TYPES])
        if path == f"{api}/field" and m == "GET":
            return httpx.Response(200, json=FIELDS)
        if path == f"{api}/issuetype" and m == "GET":
            return httpx.Response(200, json=ISSUE_TYPES)
        if path == f"{api}/priority" and m == "GET":
            return httpx.Response(200, json=PRIORITIES)
        if path == f"{api}/status" and m == "GET":
            return httpx.Response(200, json=STATUSES)
        if path == f"{api}/filter/favourite" and m == "GET":
            return httpx.Response(200, json=self.filters)
        if path == f"{api}/user/assignable/search" and m == "GET":
            return self._assignable(q)
        if path == f"{api}/search":
            if m == "GET":
                jql, start, limit = q.get("jql", ""), self._int(q, "startAt", 0), self._int(q, "maxResults", 50)
                fields = (q.get("fields") or "").split(",") if q.get("fields") else None
            else:
                body = self._body(request)
                vq = body.get("validateQuery")
                if vq is not None and not isinstance(vq, bool):
                    # Real DC (verified on 10.3) binds SearchRequestBean.validateQuery to java.lang.Boolean.
                    return httpx.Response(
                        400, headers={"content-type": "text/plain;charset=UTF-8"},
                        text=f"Cannot deserialize value of type `java.lang.Boolean` from String \"{vq}\": "
                             "only \"true\" or \"false\" recognized\n at [Source: REDACTED]")
                jql, start, limit = body.get("jql", ""), int(body.get("startAt", 0)), int(body.get("maxResults", 50))
                fields = body.get("fields")
            self.searches.append(jql)
            found = self._run_jql(jql)
            page = self._paged([self._render(i, fields) for i in found], start, limit, key="issues",
                               cap=1000, total=True, is_last=False)
            return httpx.Response(200, json=page)
        if path == f"{api}/jql/autocompletedata" and m == "GET":
            return httpx.Response(200, json=self._autocompletedata())
        if path == f"{api}/jql/autocompletedata/suggestions" and m == "GET":
            return httpx.Response(200, json={"results": self._suggestions(q.get("fieldName", ""),
                                                                          q.get("fieldValue", ""))})
        if path == f"{api}/issue" and m == "POST":
            return self._create(self._body(request))
        if path == f"{api}/issue/createmeta" and m == "GET":
            if not self.legacy_createmeta:
                return _err(404, "null for uri: /rest/api/2/issue/createmeta")
            keys = [k for k in (q.get("projectKeys") or "").split(",") if k]
            projects = [p for p in self.projects if not keys or p["key"] in keys]
            expand = "fields" in (q.get("expand") or "")
            return httpx.Response(200, json={"projects": [{
                "key": p["key"], "id": p["id"], "name": p["name"],
                "issuetypes": [dict(t, **({"fields": {f["fieldId"]: f for f in self._create_fields(p["key"], t)}}
                                          if expand else {})) for t in ISSUE_TYPES]} for p in projects]})
        mm = re.match(rf"^{api}/issue/createmeta/([^/]+)/issuetypes(?:/([^/]+))?$", path)
        if mm and m == "GET":
            if not self.new_createmeta or self.version_tuple[:2] < (8, 4):
                return _err(404, f"null for uri: {path}")
            proj = self._project(mm.group(1))
            if not proj:
                return _err(404, f"No project could be found with key '{mm.group(1)}'.")
            start, limit = self._int(q, "startAt", 0), self._int(q, "maxResults", 50)
            if mm.group(2) is None:
                return httpx.Response(200, json=self._paged(ISSUE_TYPES, start, limit, total=True))
            t = next((t for t in ISSUE_TYPES if t["id"] == mm.group(2)), None)
            if t is None:
                return _err(404, f"Issue type with id '{mm.group(2)}' does not exist")
            return httpx.Response(200, json=self._paged(self._create_fields(proj["key"], t), start, limit,
                                                        total=True))
        mm = re.match(rf"^{api}/issue/([^/]+)(/.*)?$", path)
        if mm:
            issue = self.issue(mm.group(1))
            if issue is None:
                return _err(404, "Issue Does Not Exist")
            return self._issue_route(request, issue, mm.group(2) or "")

        if path == "/rest/dev-status/latest/issue/detail" and m == "GET":
            q = request.url.params
            if q.get("applicationType") != "stash" or q.get("dataType") != "pullrequest":
                return _err(400, "Unsupported dev-status query")
            prs = next((v for k, v in PULL_REQUESTS.items() if self.issue(k) and self.issue(k)["id"] == q.get("issueId")), [])
            return httpx.Response(200, json={"errors": [], "detail": [{"pullRequests": prs}]})

        # ---------------------------------------------------------- agile
        if path == f"{agile}/board" and m == "GET":
            pk = (q.get("projectKeyOrId") or "").upper()
            boards = [b for b in self.boards if not pk or b["project"] == pk
                      or (self._project(pk) or {}).get("key") == b["project"]]
            if q.get("type"):
                boards = [b for b in boards if b["type"] == q.get("type")]
            vals = [{"id": b["id"], "name": b["name"], "type": b["type"],
                     "location": {"projectKey": b["project"], "projectId": int(self._project(b["project"])["id"])}}
                    for b in boards]
            return httpx.Response(200, json=self._paged(vals, self._int(q, "startAt", 0),
                                                        self._int(q, "maxResults", 50)))
        mm = re.match(rf"^{agile}/board/(\d+)(/.*)?$", path)
        if mm:
            board = next((b for b in self.boards if b["id"] == int(mm.group(1))), None)
            if board is None:
                return _err(404, "Board does not exist or you do not have permission to see it.")
            return self._board_route(request, board, mm.group(2) or "")
        mm = re.match(rf"^{agile}/sprint/(\d+)/issue$", path)
        if mm and m == "GET":
            s = self.sprint_by_id(int(mm.group(1)))
            if s is None:
                return _err(404, "Sprint does not exist")
            pool = [i for i in self.issues if s["id"] in i.get("_sprints", [])]
            return self._agile_issues(q, pool)
        return _err(404, f"null for uri: {path}")

    def _project(self, key_or_id: str) -> Optional[J]:
        k = key_or_id.lower()
        return next((p for p in self.projects if p["key"].lower() == k or p["id"] == key_or_id), None)

    def _assignable(self, q: Any) -> httpx.Response:
        project = q.get("project")
        issue_key = q.get("issueKey")
        if issue_key:
            issue = self.issue(issue_key)
            if issue is None:
                return _err(404, "Issue Does Not Exist")
            project = issue["fields"]["project"]["key"]
        if not project:
            return _err(400, "The parameters 'project' or 'issueKey' are required.")
        proj = self._project(project)
        if proj is None:
            return _err(404, f"No project could be found with key '{project}'.")
        term = (q.get("username") or "").lower()
        users = [USERS[n] for n in self.assignable.get(proj["key"], [])]
        if term:
            users = [u for u in users if term in u["name"].lower() or term in u["displayName"].lower()]
        start, limit = self._int(q, "startAt", 0), max(1, min(self._int(q, "maxResults", 50), 1000))
        return httpx.Response(200, json=users[start: start + limit])

    def _agile_issues(self, q: Any, pool: List[J]) -> httpx.Response:
        extra = q.get("jql") or ""
        found = self._run_jql(extra, pool) if extra else sorted(pool, key=lambda i: i["_rank"])
        fields = (q.get("fields") or "").split(",") if q.get("fields") else None
        page = self._paged([self._render(i, fields, agile=True) for i in found], self._int(q, "startAt", 0),
                           self._int(q, "maxResults", 50), key="issues", total=True, is_last=False)
        return httpx.Response(200, json=page)

    def _board_route(self, request: httpx.Request, board: J, rest: str) -> httpx.Response:
        q = request.url.params
        if rest == "/configuration":
            return httpx.Response(200, json={
                "id": board["id"], "name": board["name"], "type": board["type"],
                "estimation": {"type": "field", "field": {"fieldId": SP, "displayName": "Story Points"}}
                if board["type"] == "scrum" else None})
        if rest == "/sprint":
            if board["type"] != "scrum":
                return _err(400, "The board does not support sprints")
            states = [s for s in (q.get("state") or "").split(",") if s]
            vals = [s for s in self.sprints if s["originBoardId"] == board["id"] and (not states or s["state"] in states)]
            return httpx.Response(200, json=self._paged(vals, self._int(q, "startAt", 0),
                                                        self._int(q, "maxResults", 50)))
        if rest == "/backlog":
            if board["type"] != "scrum":
                return _err(400, "The board does not support backlog (kanban board without backlog).")
            pool = [i for i in self.issues if i["fields"]["project"]["key"] == board["project"]
                    and i["fields"]["status"]["statusCategory"]["key"] != "done"
                    and not any((self.sprint_by_id(s) or {}).get("state") in ("active", "future")
                                for s in i.get("_sprints", []))]
            return self._agile_issues(q, pool)
        return _err(404, "No such board resource")

    def _issue_route(self, request: httpx.Request, issue: J, rest: str) -> httpx.Response:
        m = request.method
        if rest == "" and m == "GET":
            fields = (request.url.params.get("fields") or "").split(",") if request.url.params.get("fields") else None
            return httpx.Response(200, json=self._render(issue, fields))
        if rest == "/transitions" and m == "GET":
            ts = WORKFLOW.get(issue["fields"]["status"]["name"], [])
            return httpx.Response(200, json={"expand": "transitions", "transitions": [
                {"id": tid, "name": name, "to": next(s for s in STATUSES if s["name"] == to)}
                for tid, name, to in ts]})
        if rest == "/transitions" and m == "POST":
            tid = str((self._body(request).get("transition") or {}).get("id") or "")
            for t_id, _name, to in WORKFLOW.get(issue["fields"]["status"]["name"], []):
                if t_id == tid:
                    st = next(s for s in STATUSES if s["name"] == to)
                    issue["fields"]["status"] = dict(st)
                    issue["fields"]["resolution"] = {"name": "Done"} if to == "Done" else None
                    return httpx.Response(204)
            return _err(400, f"Transition id '{tid}' is not valid for this issue.")
        if rest == "/comment" and m == "POST":
            body = str(self._body(request).get("body") or "")
            if not body.strip():
                return _err(400, errors={"comment": "Comment body can not be empty!"})
            c = {"id": str(99000 + len(self.comments_posted)), "author": USERS[self._current_user], "body": body,
                 "created": "2026-11-17T12:00:00.000+0000"}
            issue.setdefault("_comments", []).append(c)
            self.comments_posted.append((issue["key"], body))
            return httpx.Response(201, json=c)
        if rest == "/assignee" and m == "PUT":
            name = self._body(request).get("name")
            if name is None:
                issue["fields"]["assignee"] = None
                return httpx.Response(204)
            if name not in self.assignable.get(issue["fields"]["project"]["key"], []):
                return _err(400, errors={"assignee": f"User '{name}' cannot be assigned issues."})
            issue["fields"]["assignee"] = USERS[name]
            return httpx.Response(204)
        return _err(404, "No such issue resource")

    # ---------------------------------------------------------- createmeta
    def _create_fields(self, project: str, itype: J) -> List[J]:
        def fld(fid: str, required: bool = False, has_default: bool = False, allowed: Optional[List[J]] = None,
                name: str = "") -> J:
            base = next(f for f in FIELDS if f["id"] == fid)
            out: J = {"fieldId": fid, "name": name or base["name"], "required": required,
                      "hasDefaultValue": has_default, "schema": base["schema"],
                      "operations": ["set"], "key": fid}
            if allowed is not None:
                out["allowedValues"] = allowed
            if fid in ("assignee", "reporter"):
                out["autoCompleteUrl"] = f"{self.base_url}/rest/api/2/user/assignable/search?project={project}&username="
            return out

        name = itype["name"]
        out = [
            fld("summary", True),
            fld("issuetype", True, allowed=[itype]),
            fld("project", True, allowed=[{"key": project}]),
            fld("reporter", True, has_default=True),
            fld("description"),
            fld("priority", required=name == "Bug", has_default=name != "Bug", allowed=PRIORITIES),
            fld("labels"),
            fld("assignee"),
            fld("components", allowed=[{"id": str(30000 + i), "name": c}
                                       for i, c in enumerate(self.components.get(project, []))]),
        ]
        if name in ("Story", "Task", "Bug"):
            out += [fld(SP), fld(EPIC_LINK), fld(SPRINT)]
        if name == "Epic":
            out.append(fld(EPIC_NAME, True))
        if name == "Bug":
            out.append(fld(TEAM, True, allowed=TEAMS))
            out.append(fld("duedate"))
        if itype["subtask"]:
            out.append(fld("parent", True))
        return out

    def _create(self, body: J) -> httpx.Response:
        f = body.get("fields") or {}
        proj = self._project(str((f.get("project") or {}).get("key") or (f.get("project") or {}).get("id") or ""))
        if proj is None:
            return _err(400, errors={"project": "project is required"})
        itype = next((t for t in ISSUE_TYPES if t["id"] == str((f.get("issuetype") or {}).get("id"))
                      or t["name"] == (f.get("issuetype") or {}).get("name")), None)
        if itype is None:
            return _err(400, errors={"issuetype": "valid issue type is required"})
        meta = {x["fieldId"]: x for x in self._create_fields(proj["key"], itype)}
        errors: Dict[str, str] = {}
        for fid in f:
            if fid not in meta:
                errors[fid] = (f"Field '{fid}' cannot be set. It is not on the appropriate screen, or unknown.")
        for fid, info in meta.items():
            if info["required"] and not info["hasDefaultValue"] and f.get(fid) in (None, "", [], {}):
                errors[fid] = ("You must specify a summary of the issue." if fid == "summary"
                               else f"{info['name']} is required.")
        if f.get("priority") and str(f["priority"].get("id")) not in {p["id"] for p in PRIORITIES}:
            errors["priority"] = "Specify a valid priority."
        if f.get(TEAM) and str(f[TEAM].get("id")) not in {t["id"] for t in TEAMS}:
            errors[TEAM] = f"Option id '{f[TEAM].get('id')}' is not valid"
        for lb in f.get("labels") or []:
            if " " in str(lb):
                errors["labels"] = f"The label '{lb}' contains spaces which is invalid."
        a = (f.get("assignee") or {}).get("name") if isinstance(f.get("assignee"), dict) else None
        if a and a not in self.assignable.get(proj["key"], []):
            errors["assignee"] = f"User '{a}' cannot be assigned issues."
        if f.get(EPIC_LINK):
            epic = self.issue(str(f[EPIC_LINK]))
            if epic is None or epic["fields"]["issuetype"]["name"] != "Epic":
                errors[EPIC_LINK] = f"The issue '{f[EPIC_LINK]}' is not an epic or does not exist."
        if f.get(SPRINT) is not None:
            s = self.sprint_by_id(int(f[SPRINT])) if str(f[SPRINT]).isdigit() else None
            if s is None:
                errors[SPRINT] = f"Sprint with id {f[SPRINT]} does not exist."
            elif s["state"] == "closed":
                errors[SPRINT] = "Issue can be assigned only active or future sprints."
        if f.get(SP) is not None and not isinstance(f[SP], (int, float)):
            errors[SP] = "Operation value must be a number"
        if itype["subtask"]:
            parent = self.issue(str((f.get("parent") or {}).get("key") or ""))
            if parent is None:
                errors["parent"] = "Could not find issue by id or key."
        if errors:
            return _err(400, errors=errors)
        n = 1 + max(int(i["key"].split("-")[1]) for i in self.issues if i["fields"]["project"]["key"] == proj["key"])
        comps = [c.get("name") or next((x for i, x in enumerate(self.components.get(proj["key"], []))
                                        if str(30000 + i) == str(c.get("id"))), "")
                 for c in f.get("components") or []]
        issue = self._make_issue(proj["key"], n, str(f["summary"]), itype=itype["name"],
                                 assignee=a, reporter=self._current_user,
                                 priority=next((p["name"] for p in PRIORITIES if p["id"] == str((f.get("priority") or {}).get("id"))), "Medium"),
                                 labels=f.get("labels") or [], sp=f.get(SP), epic=f.get(EPIC_LINK) or "",
                                 description=f.get("description") or "", components=comps,
                                 parent=(f.get("parent") or {}).get("key", ""), epic_name=f.get(EPIC_NAME) or "",
                                 day=17)
        if f.get(TEAM):
            issue["fields"][TEAM] = next(t for t in TEAMS if t["id"] == str(f[TEAM]["id"]))
        issue["_sprints"] = [int(f[SPRINT])] if f.get(SPRINT) is not None else []
        issue["_comments"], issue["_links"] = [], []
        self.issues.append(issue)
        self.created.append(body)
        return httpx.Response(201, json={"id": issue["id"], "key": issue["key"], "self": issue["self"]})

    # -------------------------------------------------------- autocomplete
    def _autocompletedata(self) -> J:
        eq = ["=", "!=", "in", "not in", "is", "is not"]
        txt = ["~", "!~", "is", "is not"]
        cmp_ = ["=", "!=", ">", ">=", "<", "<=", "in", "not in", "is", "is not"]

        def fld(value: str, display: str = "", ops: Sequence[str] = eq, orderable: bool = True,
                cfid: str = "") -> J:
            d: J = {"value": value, "displayName": display or value, "orderable": "true" if orderable else "false",
                    "searchable": "true", "operators": list(ops), "types": ["java.lang.String"]}
            if cfid:
                d["cfid"] = cfid
            return d

        return {
            "visibleFieldNames": [
                fld("assignee"), fld("component"), fld("created", ops=cmp_), fld("description", ops=txt),
                fld('"Epic Link"', "Epic Link - cf[10003]", cfid="cf[10003]"), fld("issuetype"),
                fld("key", ops=cmp_), fld("labels"), fld("priority", ops=cmp_), fld("project"),
                fld("reporter"), fld("resolution"), fld("sprint", "Sprint - cf[10001]", cfid="cf[10001]"),
                fld("status"), fld("statusCategory"), fld('"Story Points"', "Story Points - cf[10002]",
                                                          ops=cmp_, cfid="cf[10002]"),
                fld("summary", ops=txt), fld("text", ops=["~"], orderable=False), fld("updated", ops=cmp_),
                fld("Rank", ops=[], orderable=True),
            ],
            "visibleFunctionNames": [{"value": v, "displayName": v} for v in (
                "currentUser()", "openSprints()", "closedSprints()", "futureSprints()", "now()", "startOfDay()",
                "membersOf(\"\")")],
            "jqlReservedWords": ["and", "or", "not", "empty", "null", "order", "by", "asc", "desc", "in", "is"],
        }

    def _suggestions(self, field: str, value: str) -> List[J]:
        f = self._alias(field.strip('"').lower())
        pool: List[Tuple[str, str]] = []
        if f == "project":
            pool = [(p["key"], f"{p['name']} ({p['key']})") for p in self.projects]
        elif f == "issuetype":
            pool = [(t["name"], t["name"]) for t in ISSUE_TYPES]
        elif f == "status":
            pool = [(s["name"], s["name"]) for s in STATUSES]
        elif f == "priority":
            pool = [(p["name"], p["name"]) for p in PRIORITIES]
        elif f == "labels":
            labels = sorted({lb for i in self.issues for lb in i["fields"]["labels"]}, key=str.lower)
            pool = [(lb, lb) for lb in labels]
        elif f in ("assignee", "reporter"):
            pool = [(u["name"], f"{u['displayName']} - {u['emailAddress']} ({u['name']})") for u in USERS.values()]
        elif f == "sprint":
            pool = [(str(s["id"]), f"{s['name']} ({s['state']})") for s in self.sprints]
        elif f == "component":
            pool = [(c, c) for cs in self.components.values() for c in cs]
        v = value.lower()
        out = []
        for val, disp in pool:
            if v and v not in val.lower() and v not in disp.lower():
                continue
            # Real Jira bolds the matched part of displayName.
            hl = re.sub("(" + re.escape(value) + ")", r"<b>\1</b>", disp, flags=re.I) if v else disp
            out.append({"value": val, "displayName": hl})
        return out[:15]
