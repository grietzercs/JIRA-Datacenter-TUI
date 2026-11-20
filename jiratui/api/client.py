"""Async Jira Server / Data Center REST client (core API 2 + Agile API 1.0).

Design goals (the opposite of an infinitely spinning UI):
* every request has connect/read/write/pool timeouts;
* idempotent requests (GETs, and searches sent as POST) are retried a small,
  bounded number of times with exponential backoff on transport errors and
  429/502/503/504; mutating requests (create, comment, transition, assign) are
  never retried automatically;
* all failures are mapped to typed :mod:`errors` with user-facing hints,
  including Jira's header-only auth failures (``X-Seraph-LoginReason``);
* paging is explicit (one page per call) so callers load lazily.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence, TypeVar
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .. import __version__
from ..redact import redact, redact_headers
from .auth import AuthStrategy
from .errors import (
    AuthenticationError,
    BadRequestError,
    ConfigError,
    JiraError,
    JqlError,
    NotFoundError,
    PermissionDeniedError,
    TLSError,
    UnexpectedResponseError,
    auth_failure_from_headers,
    error_from_response,
    error_from_transport,
)
from .models import (
    Board,
    CreateField,
    Field,
    FieldCatalog,
    Issue,
    PullRequest,
    IssueType,
    JiraFilter,
    JqlAutocompleteData,
    Page,
    Priority,
    Project,
    ServerInfo,
    Sprint,
    Status,
    Suggestion,
    Transition,
    User,
)
from .tls import TLSOptions, build_verify

log = logging.getLogger("jiratui.http")

T = TypeVar("T")
API = "/rest/api/2"
AGILE = "/rest/agile/1.0"
RETRY_STATUSES = {429, 502, 503, 504}
SPRINT_STATES = ("active", "future", "closed")

# Browser paths people paste; everything from here on is stripped.
_STRIP_MARKERS = ("/rest/", "/browse/", "/secure/", "/projects/", "/issues/", "/login.jsp", "/plugins/servlet/")


def normalize_base_url(url: str) -> str:
    """Validate and normalise a Jira base URL.

    * strips whitespace, query, fragment and trailing slashes;
    * strips a pasted REST path or browser path (``/browse/KEY-1``,
      ``/secure/Dashboard.jspa``, ``/projects/X``...) but keeps a context path
      such as ``/jira``;
    * requires an http/https scheme and a host; rejects embedded credentials.
    """
    raw = (url or "").strip()
    if not raw:
        raise ConfigError("Server URL is required")
    if "://" not in raw:
        raise ConfigError(
            f"URL must start with https:// or http:// (got {raw!r})",
            hint="Example: https://jira.example.com",
        )
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ConfigError(f"Unsupported URL scheme {parts.scheme!r}; use https:// or http://")
    if not parts.hostname:
        raise ConfigError("URL has no host name")
    if parts.username or parts.password:
        raise ConfigError(
            "Do not put credentials in the URL",
            hint="Enter the username/token in the credential fields instead.",
        )
    path = parts.path.rstrip("/")
    low = path.lower()
    cut = min((i for i in (low.find(m) for m in _STRIP_MARKERS) if i >= 0), default=-1)
    if cut >= 0:
        path = path[:cut]
    netloc = parts.hostname.lower()
    if ":" in netloc:  # IPv6 literal
        netloc = f"[{netloc}]"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((scheme, netloc, path.rstrip("/"), "", ""))


def enc(segment: Any) -> str:
    """URL-encode a single path segment (issue key, project key, id)."""
    return quote(str(segment), safe="~")


class JiraClient:
    def __init__(
        self,
        base_url: str,
        auth: Optional[AuthStrategy],
        *,
        tls: Optional[TLSOptions] = None,
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
        max_retries: int = 2,
        backoff: float = 0.5,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.auth = auth
        self.tls = tls or TLSOptions()
        self.max_retries = max(0, int(max_retries))
        self.backoff = backoff
        self.server_username: str = ""
        self.timeout = httpx.Timeout(timeout, connect=connect_timeout)
        verify = build_verify(self.tls) if transport is None else True
        headers = {
            "Accept": "application/json",
            "User-Agent": f"jira-tui/{__version__}",
        }
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
            verify=verify,
            follow_redirects=False,
            transport=transport,
            trust_env=True,
        )
        # Per-session caches for data that rarely changes.
        self._catalog: Optional[FieldCatalog] = None
        self._autocomplete: Optional[JqlAutocompleteData] = None
        self._legacy_meta: Dict[str, Dict[str, Any]] = {}
        self.createmeta_mode: str = ""  # "issuetypes" (DC 8.4+) | "legacy"

    # ------------------------------------------------------------------ core
    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "JiraClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def auth_headers(self) -> Dict[str, str]:
        return self.auth.headers() if self.auth else {}

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Any = None,
        retry: Optional[bool] = None,
        authenticated: bool = True,
    ) -> httpx.Response:
        method = method.upper()
        idempotent = method in ("GET", "HEAD") if retry is None else retry
        url = path if path.startswith("/") else "/" + path
        headers = dict(self.auth_headers()) if authenticated else {}
        if method not in ("GET", "HEAD"):
            # Jira's XSRF protection can reject mutating requests without this header.
            headers["X-Atlassian-Token"] = "no-check"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        attempts = self.max_retries + 1 if idempotent else 1
        for attempt in range(attempts):
            t0 = time.monotonic()
            try:
                resp = await self._http.request(
                    method, url, params=clean_params, json=json, headers=headers
                )
            except httpx.TransportError as exc:
                err = error_from_transport(exc, self.base_url + url)
                log.debug(
                    "%s %s -> transport error %s (attempt %d/%d)",
                    method, url, redact(str(exc)), attempt + 1, attempts,
                )
                if attempt + 1 < attempts and not isinstance(err, TLSError):
                    await asyncio.sleep(self.backoff * (2 ** attempt))
                    continue
                raise err from exc
            elapsed = time.monotonic() - t0
            log.debug(
                "%s %s params=%s -> %s in %.2fs req_headers=%s",
                method, url, clean_params, resp.status_code, elapsed,
                redact_headers({**self._http.headers, **headers}),
            )
            user = resp.headers.get("x-ausername")
            if user and user != "anonymous":
                self.server_username = user
            if resp.status_code in RETRY_STATUSES and attempt + 1 < attempts:
                delay = self.backoff * (2 ** attempt)
                ra = resp.headers.get("retry-after")
                if ra and ra.isdigit():
                    delay = min(float(ra), 10.0)
                await asyncio.sleep(delay)
                continue
            if 200 <= resp.status_code < 300:
                if authenticated and self.auth is not None:
                    # Jira can serve anonymously-readable resources with HTTP 200
                    # even though the supplied credentials were rejected.
                    header_err = auth_failure_from_headers(resp)
                    if header_err is not None:
                        raise header_err
                return resp
            raise error_from_response(resp)
        raise JiraError("Request failed after retries")  # pragma: no cover

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            ctype = resp.headers.get("content-type", "?")
            snippet = redact(resp.text[:200].replace("\n", " "))
            raise UnexpectedResponseError(
                f"Expected JSON but got {ctype}: {snippet!r}", status=resp.status_code
            )

    async def get_json(self, path: str, params: Optional[Dict[str, Any]] = None, *,
                       authenticated: bool = True) -> Any:
        return self._json(await self.request("GET", path, params=params, authenticated=authenticated))

    async def post_json(self, path: str, body: Any = None, *, retry: Optional[bool] = None) -> Any:
        return self._json(await self.request("POST", path, json=body if body is not None else {}, retry=retry))

    async def put_json(self, path: str, body: Any = None) -> Any:
        return self._json(await self.request("PUT", path, json=body if body is not None else {}))

    async def get_page(
        self,
        path: str,
        parse: Callable[[Dict[str, Any]], T],
        *,
        start: int = 0,
        limit: int = 50,
        params: Optional[Dict[str, Any]] = None,
        key: str = "values",
    ) -> Page[T]:
        p = dict(params or {})
        p["startAt"] = start
        p["maxResults"] = limit
        data = await self.get_json(path, p)
        if not isinstance(data, (dict, list)):
            raise UnexpectedResponseError("Expected a paged JSON response")
        return Page.from_json(data, parse, key=key, start_at=start, max_results=limit)

    async def iter_all(
        self,
        path: str,
        parse: Callable[[Dict[str, Any]], T],
        *,
        params: Optional[Dict[str, Any]] = None,
        limit: int = 50,
        max_items: Optional[int] = 1000,
        key: str = "values",
    ) -> AsyncIterator[T]:
        """Iterate items across pages, bounded by ``max_items`` (never unbounded by default)."""
        start: Optional[int] = 0
        count = 0
        while start is not None:
            page = await self.get_page(path, parse, start=start, limit=limit, params=params, key=key)
            for v in page.values:
                yield v
                count += 1
                if max_items is not None and count >= max_items:
                    return
            nxt = page.next_start
            start = nxt if nxt is not None and nxt > start else None

    async def collect(self, it: AsyncIterator[T]) -> List[T]:
        return [x async for x in it]

    # ------------------------------------------------------------- server
    async def server_info(self) -> ServerInfo:
        """``/serverInfo`` without credentials: Jira answers it anonymously and it
        tells us the version (PAT support needs 8.14+) and deployment type."""
        try:
            data = await self.get_json(f"{API}/serverInfo", authenticated=False)
        except (AuthenticationError, PermissionDeniedError):
            # Some instances block all anonymous access; try with credentials.
            data = await self.get_json(f"{API}/serverInfo")
        except (NotFoundError, UnexpectedResponseError) as exc:
            raise UnexpectedResponseError(
                f"{self.base_url} does not look like a Jira REST endpoint ({exc.message})",
                status=exc.status,
                hint="Use the base URL you open in the browser, e.g. https://jira.example.com "
                "(include a context path such as /jira if your instance has one).",
            ) from exc
        if not isinstance(data, dict) or "version" not in data:
            raise UnexpectedResponseError(
                f"{self.base_url}/rest/api/2/serverInfo did not return Jira server info",
                hint="Use the Jira base URL (not Confluence/Bitbucket or a proxy landing page).",
            )
        return ServerInfo.from_json(self.base_url, data)

    async def myself(self) -> User:
        data = await self.get_json(f"{API}/myself")
        if not isinstance(data, dict) or not (data.get("name") or data.get("key")):
            raise UnexpectedResponseError("/myself did not return a user")
        return User.from_json(data)

    async def test_connection(self, info: Optional[ServerInfo] = None) -> ServerInfo:
        """Probe the server (anonymous), then prove the credentials via ``/myself``."""
        info = info or await self.server_info()
        try:
            user = await self.myself()
        except AuthenticationError as exc:
            if self.auth is not None and self.auth.method == "bearer" and info.supports_pat is False:
                exc.hint = (
                    f"This server reports {info.describe()}; personal access tokens (Bearer) need "
                    "Jira Data Center/Server 8.14 or newer. Use Basic with username + password."
                )
            raise
        info.user = user
        info.username = user.name
        if info.is_cloud:
            info.warnings.append("This is Jira Cloud; jira-tui targets Server/Data Center. "
                                 "Some features (usernames in JQL) behave differently on Cloud.")
        if self.auth is not None and self.auth.username and self.auth.username.lower() != user.name.lower():
            info.warnings.append(f"Signed in as {user.name!r}, not the username entered ({self.auth.username!r}).")
        return info

    # ----------------------------------------------------------- metadata
    async def list_projects(self) -> List[Project]:
        data = await self.get_json(f"{API}/project")
        return [Project.from_json(p) for p in data or [] if isinstance(p, dict)]

    async def fields(self) -> FieldCatalog:
        if self._catalog is None:
            data = await self.get_json(f"{API}/field")
            self._catalog = FieldCatalog.from_fields([Field.from_json(f) for f in data or [] if isinstance(f, dict)])
        return self._catalog

    async def issue_types(self) -> List[IssueType]:
        data = await self.get_json(f"{API}/issuetype")
        return [IssueType.from_json(t) for t in data or [] if isinstance(t, dict)]

    async def priorities(self) -> List[Priority]:
        data = await self.get_json(f"{API}/priority")
        return [Priority.from_json(p) for p in data or [] if isinstance(p, dict)]

    async def statuses(self, project_key: str = "") -> List[Status]:
        """Statuses used by a project (deduplicated across issue types), or all."""
        if project_key:
            try:
                data = await self.get_json(f"{API}/project/{enc(project_key)}/statuses")
                seen: Dict[str, Status] = {}
                for it in data or []:
                    for s in (it or {}).get("statuses") or []:
                        st = Status.from_json(s)
                        seen.setdefault(st.name.lower(), st)
                if seen:
                    return list(seen.values())
            except (NotFoundError, PermissionDeniedError) as exc:
                log.debug("project statuses unavailable: %s", exc.message)
        data = await self.get_json(f"{API}/status")
        return [Status.from_json(s) for s in data or [] if isinstance(s, dict)]

    async def favourite_filters(self) -> List[JiraFilter]:
        data = await self.get_json(f"{API}/filter/favourite")
        return [JiraFilter.from_json(f) for f in data or [] if isinstance(f, dict)]

    # ---------------------------------------------------------------- agile
    async def list_boards(self, project_key: str = "", start: int = 0, limit: int = 50,
                          board_type: str = "") -> Page[Board]:
        return await self.get_page(
            f"{AGILE}/board", Board.from_json, start=start, limit=limit,
            params={"projectKeyOrId": project_key or None, "type": board_type or None},
        )

    async def all_boards(self, project_key: str, max_items: int = 200) -> List[Board]:
        return await self.collect(self.iter_all(f"{AGILE}/board", Board.from_json,
                                                params={"projectKeyOrId": project_key}, max_items=max_items))

    async def board_estimation_field(self, board_id: int) -> str:
        """The board's estimation field id (usually Story Points), or ''."""
        try:
            data = await self.get_json(f"{AGILE}/board/{int(board_id)}/configuration")
        except (NotFoundError, PermissionDeniedError, BadRequestError) as exc:
            log.debug("board configuration unavailable: %s", exc.message)
            return ""
        est = (data or {}).get("estimation") or {}
        fid = str(((est.get("field") or {}).get("fieldId")) or "")
        return fid if fid.startswith("customfield_") else ""

    async def list_sprints(self, board_id: int, states: Sequence[str] = ("active", "future"),
                           start: int = 0, limit: int = 50) -> Page[Sprint]:
        return await self.get_page(
            f"{AGILE}/board/{int(board_id)}/sprint", Sprint.from_json, start=start, limit=limit,
            params={"state": ",".join(states) or None},
        )

    async def board_sprints(self, board_id: int, include_closed: bool = False,
                            max_closed: int = 200) -> List[Sprint]:
        """Active first, then future (in board order), then closed (most recent first)."""
        path = f"{AGILE}/board/{int(board_id)}/sprint"
        open_ = await self.collect(self.iter_all(path, Sprint.from_json, params={"state": "active,future"},
                                                 max_items=500))
        open_.sort(key=lambda s: 0 if s.state == "active" else 1)  # stable: keeps board order
        if not include_closed:
            return open_
        closed = await self.collect(self.iter_all(path, Sprint.from_json, params={"state": "closed"},
                                                  max_items=2000))
        closed = sorted(closed, key=lambda s: (s.complete_date or s.end_date or "", s.id), reverse=True)
        return open_ + closed[:max_closed]

    async def board_backlog(self, board_id: int, jql: str = "", start: int = 0, limit: int = 50,
                            fields: Sequence[str] = (), catalog: Optional[FieldCatalog] = None,
                            story_points_id: str = "") -> Page[Issue]:
        """Backlog of a scrum board (``/board/{id}/backlog``); ``jql`` is ANDed by Jira."""
        try:
            return await self.get_page(
                f"{AGILE}/board/{int(board_id)}/backlog",
                lambda d: Issue.from_json(d, catalog, story_points_id),
                start=start, limit=limit, key="issues",
                params={"jql": jql or None, "fields": ",".join(fields) or None},
            )
        except BadRequestError as exc:
            raise self._jql_error(exc) from exc

    async def sprint_issues(self, sprint_id: int, jql: str = "", start: int = 0, limit: int = 50,
                            fields: Sequence[str] = (), catalog: Optional[FieldCatalog] = None,
                            story_points_id: str = "") -> Page[Issue]:
        """Issues of a sprint via the Agile API (used when JQL ``sprint =`` is not usable)."""
        try:
            return await self.get_page(
                f"{AGILE}/sprint/{int(sprint_id)}/issue",
                lambda d: Issue.from_json(d, catalog, story_points_id),
                start=start, limit=limit, key="issues",
                params={"jql": jql or None, "fields": ",".join(fields) or None},
            )
        except BadRequestError as exc:
            raise self._jql_error(exc) from exc

    async def sprint_assignees(self, sprint_id: int, max_issues: int = 1000) -> List[User]:
        """Distinct assignees of a sprint's issues (aggregated from a bounded Agile API read)."""
        users: Dict[str, User] = {}
        start = 0
        while start < max_issues:
            page = await self.sprint_issues(sprint_id, "", start, min(100, max_issues - start), fields=["assignee"])
            users.update({i.assignee.name: i.assignee for i in page.values if i.assignee} | users)
            if page.next_start is None or page.next_start <= start:
                break
            start = page.next_start
        return list(users.values())

    # --------------------------------------------------------------- search
    @staticmethod
    def _jql_error(exc: BadRequestError) -> JqlError:
        msg = "; ".join(exc.error_messages) or exc.message
        return JqlError(msg, status=exc.status, error_messages=exc.error_messages, field_errors=exc.field_errors)

    async def search(self, jql: str, start: int = 0, limit: int = 50, fields: Sequence[str] = (),
                     catalog: Optional[FieldCatalog] = None, story_points_id: str = "") -> Page[Issue]:
        """``POST /search`` (long JQL never hits URL limits). Searching is
        idempotent, so it is retried like a GET.

        ``validateQuery`` must be a JSON boolean: Server/DC deserialises it as
        ``java.lang.Boolean`` and answers Cloud's ``"strict"`` with HTTP 400."""
        body: Dict[str, Any] = {"jql": jql, "startAt": int(start), "maxResults": int(limit),
                                "validateQuery": True}
        if fields:
            body["fields"] = list(fields)
        try:
            data = await self.post_json(f"{API}/search", body, retry=True)
        except BadRequestError as exc:
            raise self._jql_error(exc) from exc
        if not isinstance(data, dict) or "issues" not in data:
            raise UnexpectedResponseError("Search response had no 'issues'")
        return Page.from_json(data, lambda d: Issue.from_json(d, catalog, story_points_id), key="issues",
                              start_at=start, max_results=limit)

    async def validate_jql(self, jql: str) -> int:
        """Run the query with ``maxResults=0``; returns the total or raises :class:`JqlError`."""
        page = await self.search(jql, 0, 0, fields=["key"])
        return int(page.total or 0)

    async def project_labels(self, project_key: str, max_issues: int = 1000) -> List[str]:
        """Labels actually used in a project (aggregated from a bounded search)."""
        from ..jql import quote_value

        labels: Dict[str, str] = {}
        start, seen = 0, 0
        jql = f"project = {quote_value(project_key)} AND labels is not EMPTY"
        while seen < max_issues:
            page = await self.search(jql, start, min(100, max_issues - seen), fields=["labels"])
            for issue in page.values:
                for lb in issue.labels:
                    labels.setdefault(lb.lower(), lb)
            seen += len(page.values)
            if page.next_start is None or page.next_start <= start:
                break
            start = page.next_start
        return sorted(labels.values(), key=str.lower)

    async def jql_autocomplete(self) -> JqlAutocompleteData:
        if self._autocomplete is None:
            data = await self.get_json(f"{API}/jql/autocompletedata")
            self._autocomplete = JqlAutocompleteData.from_json(data or {})
        return self._autocomplete

    async def jql_suggestions(self, field_name: str, value: str = "") -> List[Suggestion]:
        data = await self.get_json(f"{API}/jql/autocompletedata/suggestions",
                                   {"fieldName": field_name, "fieldValue": value})
        return [Suggestion.from_json(r) for r in (data or {}).get("results") or [] if isinstance(r, dict)]

    async def label_suggestions(self, prefix: str = "") -> List[str]:
        return [s.value for s in await self.jql_suggestions("labels", prefix)]

    # ---------------------------------------------------------------- users
    async def assignable_users(self, project_key: str = "", issue_key: str = "", query: str = "",
                               max_items: int = 1000) -> List[User]:
        """``/user/assignable/search`` (plain-array paging by startAt/maxResults)."""
        if not project_key and not issue_key:
            raise ConfigError("assignable_users needs a project or an issue")
        users = await self.collect(self.iter_all(
            f"{API}/user/assignable/search", User.from_json,
            params={"project": project_key or None, "issueKey": issue_key or None, "username": query or None},
            limit=100, max_items=max_items,
        ))
        seen = set()
        out = []
        for u in users:
            if u.name not in seen and u.active:
                seen.add(u.name)
                out.append(u)
        return out

    # --------------------------------------------------------------- issues
    async def get_issue(self, key: str, catalog: Optional[FieldCatalog] = None,
                        story_points_id: str = "") -> Issue:
        data = await self.get_json(f"{API}/issue/{enc(key)}")
        return Issue.from_json(data or {}, catalog, story_points_id)

    async def pull_requests(self, issue_id: str) -> List[PullRequest]:
        """Pull requests that Jira's Bitbucket Server (``stash``) integration links to an issue."""
        data = await self.get_json("/rest/dev-status/latest/issue/detail", params={
            "issueId": issue_id, "applicationType": "stash", "dataType": "pullrequest"})
        return [PullRequest.from_json(pr) for d in (data or {}).get("detail") or []
                for pr in d.get("pullRequests") or [] if isinstance(pr, dict)]

    async def transitions(self, key: str) -> List[Transition]:
        data = await self.get_json(
            f"{API}/issue/{enc(key)}/transitions", {"includeUnavailableTransitions": "true"})
        return [Transition.from_json(t) for t in (data or {}).get("transitions") or [] if isinstance(t, dict)]

    async def transition(self, key: str, transition_id: str) -> None:
        await self.post_json(f"{API}/issue/{enc(key)}/transitions", {"transition": {"id": str(transition_id)}})

    async def add_comment(self, key: str, body: str) -> Dict[str, Any]:
        if not body.strip():
            raise ConfigError("Comment text is empty")
        return await self.post_json(f"{API}/issue/{enc(key)}/comment", {"body": body})

    async def assign(self, key: str, username: Optional[str]) -> None:
        """Assign to ``username`` (Server/DC uses ``name``); ``None`` unassigns."""
        await self.put_json(f"{API}/issue/{enc(key)}/assignee", {"name": username})

    # ------------------------------------------------------------ createmeta
    async def createmeta_issue_types(self, project_key: str) -> List[IssueType]:
        """Issue types that can be created in a project.

        Uses the DC 8.4+ ``/issue/createmeta/{project}/issuetypes`` endpoint and
        falls back to the legacy ``/issue/createmeta?expand=...`` (removed in Jira 9).
        """
        try:
            types = await self.collect(self.iter_all(
                f"{API}/issue/createmeta/{enc(project_key)}/issuetypes", IssueType.from_json, max_items=500))
            self.createmeta_mode = "issuetypes"
            return types
        except NotFoundError:
            pass
        meta = await self._legacy_createmeta(project_key)
        self.createmeta_mode = "legacy"
        return [IssueType.from_json(t) for t in meta.get("issuetypes") or []]

    async def _legacy_createmeta(self, project_key: str) -> Dict[str, Any]:
        if project_key not in self._legacy_meta:
            data = await self.get_json(f"{API}/issue/createmeta",
                                       {"projectKeys": project_key, "expand": "projects.issuetypes.fields"})
            projects = (data or {}).get("projects") or []
            match = [p for p in projects if str(p.get("key", "")).lower() == project_key.lower()]
            if not match:
                raise NotFoundError(f"Project {project_key} not found or you cannot create issues in it")
            self._legacy_meta[project_key] = match[0]
        return self._legacy_meta[project_key]

    async def createmeta_fields(self, project_key: str, issue_type_id: str) -> List[CreateField]:
        if self.createmeta_mode != "legacy":
            try:
                return await self.collect(self.iter_all(
                    f"{API}/issue/createmeta/{enc(project_key)}/issuetypes/{enc(issue_type_id)}",
                    CreateField.from_json, max_items=1000))
            except NotFoundError:
                if self.createmeta_mode == "issuetypes":
                    raise
        meta = await self._legacy_createmeta(project_key)
        for t in meta.get("issuetypes") or []:
            if str(t.get("id")) == str(issue_type_id):
                return [CreateField.from_json(v, k) for k, v in (t.get("fields") or {}).items()]
        raise NotFoundError(f"Issue type {issue_type_id} is not creatable in {project_key}")

    async def create_issue(self, fields: Dict[str, Any]) -> str:
        """``POST /issue``; returns the new key. Field errors come back as
        :class:`BadRequestError` with ``field_errors``."""
        data = await self.post_json(f"{API}/issue", {"fields": fields})
        key = str((data or {}).get("key") or "")
        if not key:
            raise UnexpectedResponseError("Create succeeded but no issue key was returned")
        return key

    def browse_url(self, key: str) -> str:
        return f"{self.base_url}/browse/{enc(key)}"
