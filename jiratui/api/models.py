"""Typed models for the subset of the Jira Server / DC REST API we use.

Covers the core API (``/rest/api/2``) and the Agile API (``/rest/agile/1.0``).
Parsing is deliberately lenient: unknown/missing fields get defaults so that
differences between Jira versions (and custom field setups) do not crash the UI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generic, List, Optional, Sequence, Tuple, TypeVar

T = TypeVar("T")
J = Dict[str, Any]

# Well-known custom field type keys (Jira Software / GreenHopper).
CF_SPRINT = "com.pyxis.greenhopper.jira:gh-sprint"
CF_EPIC_LINK = "com.pyxis.greenhopper.jira:gh-epic-link"
CF_EPIC_NAME = "com.pyxis.greenhopper.jira:gh-epic-label"
STORY_POINT_NAMES = ("story points", "story point estimate", "story point")


def format_ts(value: Any) -> str:
    """``2024-01-15T10:20:30.000+0000`` -> ``2024-01-15 10:20`` (no tz maths)."""
    s = str(value or "")
    if len(s) >= 16 and s[4] == "-" and s[10] in "T ":
        return f"{s[:10]} {s[11:16]}"
    return s[:16]


def format_date(value: Any) -> str:
    s = str(value or "")
    return s[:10] if len(s) >= 10 else s


def parse_version(text: str) -> Tuple[int, ...]:
    nums = []
    for part in re.split(r"[.\-]", str(text or "")):
        if part.isdigit():
            nums.append(int(part))
        else:
            break
    return tuple(nums)


def strip_html(text: str) -> str:
    """Jira suggestion ``displayName`` values contain ``<b>`` highlight tags."""
    text = re.sub(r"<[^>]+>", "", str(text or ""))
    return (text.replace("&quot;", '"').replace("&#39;", "'").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&amp;", "&"))


# ------------------------------------------------------------------- paging
@dataclass
class Page(Generic[T]):
    """One page of a Jira paged response.

    Jira has three paging styles, all ``startAt``/``maxResults`` based:
    Agile lists (``isLast`` + ``values``), searches (``total`` + ``issues``) and
    plain JSON arrays (no metadata: a short page means the end).
    """

    values: List[T]
    start_at: int = 0
    max_results: int = 50
    total: Optional[int] = None
    is_last: bool = True

    @property
    def next_start(self) -> Optional[int]:
        return None if self.is_last else self.start_at + len(self.values)

    @classmethod
    def from_json(cls, data: Any, parse: Callable[[J], T], key: str = "values",
                  start_at: int = 0, max_results: int = 50) -> "Page[T]":
        if isinstance(data, list):  # plain array endpoints (e.g. user/assignable/search)
            raw = data
            meta: J = {}
        elif isinstance(data, dict):
            raw = data.get(key) or []
            meta = data
        else:
            raw, meta = [], {}
        values = [parse(v) for v in raw if isinstance(v, dict)]
        start = int(meta.get("startAt", start_at) or 0)
        maxr = int(meta.get("maxResults", max_results) or max_results or 0)
        total = meta.get("total")
        total = int(total) if isinstance(total, (int, float)) else None
        if "isLast" in meta:
            is_last = bool(meta.get("isLast"))
        elif total is not None:
            is_last = start + len(values) >= total
        else:
            is_last = maxr <= 0 or len(raw) < maxr
        if not values:
            # A non-last page without values would loop forever; treat as last.
            is_last = True
        return cls(values=values, start_at=start, max_results=maxr, total=total, is_last=is_last)


# -------------------------------------------------------------------- users
@dataclass
class User:
    name: str  # username: what JQL and the assignee API use on Server/DC
    key: str = ""
    display_name: str = ""
    email: str = ""
    active: bool = True

    @classmethod
    def from_json(cls, d: Optional[J]) -> "User":
        d = d or {}
        name = str(d.get("name") or d.get("key") or "")
        return cls(
            name=name,
            key=str(d.get("key") or name),
            display_name=str(d.get("displayName") or name),
            email=str(d.get("emailAddress") or ""),
            active=bool(d.get("active", True)),
        )

    @classmethod
    def maybe(cls, d: Any) -> Optional["User"]:
        return cls.from_json(d) if isinstance(d, dict) and (d.get("name") or d.get("key")) else None

    def label(self) -> str:
        if self.display_name and self.display_name != self.name:
            return f"{self.display_name} ({self.name})"
        return self.name or "?"


@dataclass
class ServerInfo:
    base_url: str
    version: str = ""
    version_numbers: Tuple[int, ...] = ()
    deployment_type: str = ""  # Server | DataCenter | Cloud
    server_title: str = ""
    build_number: str = ""
    username: str = ""
    user: Optional[User] = None
    warnings: List[str] = field(default_factory=list)

    PAT_MIN_VERSION = (8, 14)

    @classmethod
    def from_json(cls, base_url: str, d: J) -> "ServerInfo":
        version = str(d.get("version") or "")
        nums = d.get("versionNumbers")
        vn = tuple(int(n) for n in nums if isinstance(n, int)) if isinstance(nums, list) else parse_version(version)
        return cls(
            base_url=base_url,
            version=version,
            version_numbers=vn,
            deployment_type=str(d.get("deploymentType") or ""),
            server_title=str(d.get("serverTitle") or ""),
            build_number=str(d.get("buildNumber") or ""),
        )

    @property
    def is_cloud(self) -> bool:
        return self.deployment_type.lower() == "cloud"

    @property
    def supports_pat(self) -> Optional[bool]:
        """True/False if the version is known, ``None`` if it could not be determined."""
        if self.is_cloud:
            return False
        if not self.version_numbers:
            return None
        return tuple(self.version_numbers[:2]) >= self.PAT_MIN_VERSION

    def describe(self) -> str:
        bits = [f"Jira {self.version or '?'}"]
        if self.deployment_type:
            bits.append(self.deployment_type)
        if self.server_title:
            bits.append(repr(self.server_title))
        return " · ".join(bits)


# ------------------------------------------------------------ project/agile
@dataclass
class Project:
    key: str
    name: str
    id: str = ""
    project_type: str = ""

    @classmethod
    def from_json(cls, d: J) -> "Project":
        return cls(
            key=str(d.get("key") or ""),
            name=str(d.get("name") or d.get("key") or ""),
            id=str(d.get("id") or ""),
            project_type=str(d.get("projectTypeKey") or ""),
        )

    def label(self) -> str:
        return f"{self.key}: {self.name}"


@dataclass
class Board:
    id: int
    name: str
    type: str = "scrum"  # scrum | kanban | simple
    project_key: str = ""

    @classmethod
    def from_json(cls, d: J) -> "Board":
        loc = d.get("location") or {}
        return cls(
            id=int(d.get("id") or 0),
            name=str(d.get("name") or ""),
            type=str(d.get("type") or "scrum").lower(),
            project_key=str(loc.get("projectKey") or ""),
        )

    @property
    def has_sprints(self) -> bool:
        return self.type == "scrum"

    def label(self) -> str:
        return f"{self.name} ({self.type}, #{self.id})"


SPRINT_STATE_ORDER = {"active": 0, "future": 1, "closed": 2}


@dataclass
class Sprint:
    id: int
    name: str
    state: str = "future"  # active | future | closed
    start_date: str = ""
    end_date: str = ""
    complete_date: str = ""
    goal: str = ""
    board_id: int = 0

    @classmethod
    def from_json(cls, d: J) -> "Sprint":
        return cls(
            id=int(d.get("id") or 0),
            name=str(d.get("name") or ""),
            state=str(d.get("state") or "future").lower(),
            start_date=str(d.get("startDate") or ""),
            end_date=str(d.get("endDate") or ""),
            complete_date=str(d.get("completeDate") or ""),
            goal=str(d.get("goal") or ""),
            board_id=int(d.get("originBoardId") or d.get("rapidViewId") or 0),
        )

    def label(self) -> str:
        return f"{self.name} [{self.state}]"


_SPRINT_NAME_RE = re.compile(
    r"\bname=(.*?),(?:startDate|endDate|completeDate|activatedDate|sequence|goal|rapidViewId|autoStartStop|synced|state)="
)


def parse_sprint_value(v: Any) -> Optional[Sprint]:
    """Parse a sprint custom-field value.

    The Agile API returns objects; the core API on many DC versions returns the
    legacy ``com.atlassian.greenhopper.service.sprint.Sprint@1f[id=12,...,name=Sprint 12,...]`` string.
    """
    if isinstance(v, dict):
        return Sprint.from_json(v)
    if not isinstance(v, str) or "id=" not in v:
        return None
    sid = re.search(r"\bid=(\d+)", v)
    state = re.search(r"\bstate=([A-Za-z]+)", v)
    name = _SPRINT_NAME_RE.search(v) or re.search(r"\bname=([^,\]]*)", v)
    return Sprint(
        id=int(sid.group(1)) if sid else 0,
        name=name.group(1) if name else "?",
        state=(state.group(1) if state else "").lower(),
    )


@dataclass
class IssueType:
    id: str
    name: str
    subtask: bool = False
    description: str = ""

    @classmethod
    def from_json(cls, d: Optional[J]) -> "IssueType":
        d = d or {}
        return cls(id=str(d.get("id") or ""), name=str(d.get("name") or ""),
                   subtask=bool(d.get("subtask", False)), description=str(d.get("description") or ""))


@dataclass
class Status:
    id: str
    name: str
    category_key: str = ""  # new | indeterminate | done
    category_name: str = ""

    @classmethod
    def from_json(cls, d: Optional[J]) -> "Status":
        d = d or {}
        cat = d.get("statusCategory") or {}
        return cls(id=str(d.get("id") or ""), name=str(d.get("name") or ""),
                   category_key=str(cat.get("key") or ""), category_name=str(cat.get("name") or ""))


@dataclass
class Priority:
    id: str
    name: str

    @classmethod
    def from_json(cls, d: Optional[J]) -> "Priority":
        d = d or {}
        return cls(id=str(d.get("id") or ""), name=str(d.get("name") or ""))


# -------------------------------------------------------------------- fields
@dataclass
class Field:
    id: str
    name: str
    custom: bool = False
    schema: J = field(default_factory=dict)
    clause_names: List[str] = field(default_factory=list)
    orderable: bool = False
    searchable: bool = False

    @classmethod
    def from_json(cls, d: J) -> "Field":
        return cls(
            id=str(d.get("id") or ""),
            name=str(d.get("name") or ""),
            custom=bool(d.get("custom", False)),
            schema=dict(d.get("schema") or {}),
            clause_names=[str(c) for c in d.get("clauseNames") or []],
            orderable=bool(d.get("orderable", False)),
            searchable=bool(d.get("searchable", False)),
        )

    @property
    def custom_type(self) -> str:
        return str(self.schema.get("custom") or "")


@dataclass
class FieldCatalog:
    """Field id discovery: story points, sprint and epic link are custom fields
    whose ids differ per Jira instance."""

    fields: List[Field] = field(default_factory=list)
    story_points_id: str = ""
    sprint_id: str = ""
    epic_link_id: str = ""
    epic_name_id: str = ""

    @classmethod
    def from_fields(cls, fields_: Sequence[Field]) -> "FieldCatalog":
        cat = cls(fields=list(fields_))
        for f in fields_:
            ct = f.custom_type
            lname = f.name.strip().lower()
            if ct == CF_SPRINT and not cat.sprint_id:
                cat.sprint_id = f.id
            elif ct == CF_EPIC_LINK and not cat.epic_link_id:
                cat.epic_link_id = f.id
            elif ct == CF_EPIC_NAME and not cat.epic_name_id:
                cat.epic_name_id = f.id
            elif lname in STORY_POINT_NAMES and f.custom and not cat.story_points_id:
                cat.story_points_id = f.id
        # Name-based fallbacks for instances without the GreenHopper type keys.
        for f in fields_:
            lname = f.name.strip().lower()
            if not cat.sprint_id and lname == "sprint" and f.custom:
                cat.sprint_id = f.id
            if not cat.epic_link_id and lname == "epic link" and f.custom:
                cat.epic_link_id = f.id
        return cat

    def name_of(self, field_id: str) -> str:
        for f in self.fields:
            if f.id == field_id:
                return f.name
        return field_id

    def issue_fields(self, extra: Sequence[str] = ()) -> List[str]:
        """``fields=`` list for issue tables (keeps responses small)."""
        out = ["summary", "issuetype", "status", "assignee", "priority", "labels", "project", "updated"]
        for fid in (self.story_points_id, self.sprint_id, self.epic_link_id, *extra):
            if fid and fid not in out:
                out.append(fid)
        return out


# -------------------------------------------------------------------- issues
@dataclass
class Comment:
    id: str
    author: User
    body: str
    created: str = ""

    @classmethod
    def from_json(cls, d: J) -> "Comment":
        return cls(id=str(d.get("id") or ""), author=User.from_json(d.get("author")),
                   body=str(d.get("body") or ""), created=str(d.get("created") or ""))


@dataclass
class IssueRef:
    key: str
    summary: str = ""
    status: str = ""
    type: str = ""

    @classmethod
    def from_json(cls, d: Optional[J]) -> "IssueRef":
        d = d or {}
        f = d.get("fields") or {}
        return cls(key=str(d.get("key") or ""), summary=str(f.get("summary") or ""),
                   status=str((f.get("status") or {}).get("name") or ""),
                   type=str((f.get("issuetype") or {}).get("name") or ""))


@dataclass
class IssueLink:
    relation: str  # e.g. "blocks", "is blocked by"
    issue: IssueRef

    @classmethod
    def from_json(cls, d: J) -> Optional["IssueLink"]:
        t = d.get("type") or {}
        if d.get("outwardIssue"):
            return cls(relation=str(t.get("outward") or t.get("name") or "relates to"),
                       issue=IssueRef.from_json(d["outwardIssue"]))
        if d.get("inwardIssue"):
            return cls(relation=str(t.get("inward") or t.get("name") or "relates to"),
                       issue=IssueRef.from_json(d["inwardIssue"]))
        return None


@dataclass
class PullRequest:
    id: str
    title: str
    url: str = ""
    status: str = ""  # OPEN, MERGED, DECLINED
    author: str = ""
    source: str = ""
    destination: str = ""

    @classmethod
    def from_json(cls, d: J) -> "PullRequest":
        def branch(b: Any) -> str:
            return str((b or {}).get("branch") or "") if isinstance(b, dict) else ""

        return cls(id=str(d.get("id") or ""), title=str(d.get("name") or ""), url=str(d.get("url") or ""),
                   status=str(d.get("status") or ""), author=str((d.get("author") or {}).get("name") or ""),
                   source=branch(d.get("source")), destination=branch(d.get("destination")))


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v)) if v not in (None, "") else None
    except ValueError:
        return None


def format_points(v: Optional[float]) -> str:
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


@dataclass
class Issue:
    key: str
    id: str = ""
    summary: str = ""
    issue_type: IssueType = field(default_factory=lambda: IssueType("", ""))
    status: Status = field(default_factory=lambda: Status("", ""))
    assignee: Optional[User] = None
    reporter: Optional[User] = None
    priority: Optional[Priority] = None
    labels: List[str] = field(default_factory=list)
    components: List[str] = field(default_factory=list)
    fix_versions: List[str] = field(default_factory=list)
    story_points: Optional[float] = None
    sprints: List[Sprint] = field(default_factory=list)
    epic_link: str = ""
    description: str = ""
    resolution: str = ""
    project_key: str = ""
    project_name: str = ""
    created: str = ""
    updated: str = ""
    parent: Optional[IssueRef] = None
    subtasks: List[IssueRef] = field(default_factory=list)
    links: List[IssueLink] = field(default_factory=list)
    comments: List[Comment] = field(default_factory=list)
    comment_total: int = 0
    fields: J = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: J, catalog: Optional[FieldCatalog] = None, story_points_id: str = "") -> "Issue":
        f = d.get("fields") or {}
        cat = catalog or FieldCatalog()
        sp_id = story_points_id or cat.story_points_id
        sprints: List[Sprint] = []
        raw_sprint: Any = None
        if cat.sprint_id and cat.sprint_id in f:
            raw_sprint = f.get(cat.sprint_id)
        elif "sprint" in f:  # Agile API adds a ``sprint`` (current) field
            raw_sprint = f.get("sprint")
        if isinstance(raw_sprint, (list, tuple)):
            sprints = [s for s in (parse_sprint_value(v) for v in raw_sprint) if s]
        elif raw_sprint is not None:
            s = parse_sprint_value(raw_sprint)
            sprints = [s] if s else []
        comment = f.get("comment") or {}
        proj = f.get("project") or {}
        links = [ln for ln in (IssueLink.from_json(x) for x in f.get("issuelinks") or [] if isinstance(x, dict)) if ln]
        return cls(
            key=str(d.get("key") or ""),
            id=str(d.get("id") or ""),
            summary=str(f.get("summary") or ""),
            issue_type=IssueType.from_json(f.get("issuetype")),
            status=Status.from_json(f.get("status")),
            assignee=User.maybe(f.get("assignee")),
            reporter=User.maybe(f.get("reporter")),
            priority=Priority.from_json(f["priority"]) if isinstance(f.get("priority"), dict) else None,
            labels=[str(x) for x in f.get("labels") or []],
            components=[str(c.get("name") or "") for c in f.get("components") or [] if isinstance(c, dict)],
            fix_versions=[str(c.get("name") or "") for c in f.get("fixVersions") or [] if isinstance(c, dict)],
            story_points=_num(f.get(sp_id)) if sp_id else None,
            sprints=sprints,
            epic_link=str(f.get(cat.epic_link_id) or "") if cat.epic_link_id else "",
            description=str(f.get("description") or ""),
            resolution=str((f.get("resolution") or {}).get("name") or "") if isinstance(f.get("resolution"), dict) else "",
            project_key=str(proj.get("key") or ""),
            project_name=str(proj.get("name") or ""),
            created=str(f.get("created") or ""),
            updated=str(f.get("updated") or ""),
            parent=IssueRef.from_json(f["parent"]) if isinstance(f.get("parent"), dict) else None,
            subtasks=[IssueRef.from_json(s) for s in f.get("subtasks") or [] if isinstance(s, dict)],
            links=links,
            comments=[Comment.from_json(c) for c in comment.get("comments") or [] if isinstance(c, dict)],
            comment_total=int(comment.get("total") or len(comment.get("comments") or [])),
            fields=f,
        )

    @property
    def sprint_label(self) -> str:
        if not self.sprints:
            return ""
        # Prefer the active/future sprint over closed history.
        ordered = sorted(self.sprints, key=lambda s: SPRINT_STATE_ORDER.get(s.state, 3))
        return ", ".join(s.name for s in ordered)


@dataclass
class Transition:
    id: str
    name: str
    to_status: str = ""

    @classmethod
    def from_json(cls, d: J) -> "Transition":
        return cls(id=str(d.get("id") or ""), name=str(d.get("name") or ""),
                   to_status=str((d.get("to") or {}).get("name") or ""))


@dataclass
class JiraFilter:
    """A saved filter stored in Jira (``/rest/api/2/filter/favourite``)."""

    id: str
    name: str
    jql: str
    owner: str = ""

    @classmethod
    def from_json(cls, d: J) -> "JiraFilter":
        owner = d.get("owner") or {}
        return cls(id=str(d.get("id") or ""), name=str(d.get("name") or ""), jql=str(d.get("jql") or ""),
                   owner=str(owner.get("displayName") or owner.get("name") or ""))


# ------------------------------------------------------------------ createmeta
@dataclass
class AllowedValue:
    id: str
    label: str
    raw: J = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: J) -> "AllowedValue":
        label = d.get("name") or d.get("value") or d.get("key") or d.get("id") or ""
        return cls(id=str(d.get("id") or ""), label=str(label), raw=d)


@dataclass
class CreateField:
    field_id: str
    name: str
    required: bool = False
    has_default: bool = False
    schema: J = field(default_factory=dict)
    allowed_values: List[AllowedValue] = field(default_factory=list)
    auto_complete_url: str = ""

    @classmethod
    def from_json(cls, d: J, field_id: str = "") -> "CreateField":
        return cls(
            field_id=str(d.get("fieldId") or d.get("key") or field_id),
            name=str(d.get("name") or field_id),
            required=bool(d.get("required", False)),
            has_default=bool(d.get("hasDefaultValue", False)),
            schema=dict(d.get("schema") or {}),
            allowed_values=[AllowedValue.from_json(v) for v in d.get("allowedValues") or [] if isinstance(v, dict)],
            auto_complete_url=str(d.get("autoCompleteUrl") or ""),
        )

    @property
    def type(self) -> str:
        return str(self.schema.get("type") or "")

    @property
    def items(self) -> str:
        return str(self.schema.get("items") or "")

    @property
    def system(self) -> str:
        return str(self.schema.get("system") or "")

    @property
    def custom_type(self) -> str:
        return str(self.schema.get("custom") or "")

    @property
    def needs_input(self) -> bool:
        return self.required and not self.has_default


# --------------------------------------------------------------- autocomplete
@dataclass
class JqlFieldInfo:
    value: str  # what to type in JQL (may be quoted / cf[NNN])
    display_name: str
    orderable: bool = False
    searchable: bool = True
    operators: List[str] = field(default_factory=list)
    types: List[str] = field(default_factory=list)
    cfid: str = ""

    @classmethod
    def from_json(cls, d: J) -> "JqlFieldInfo":
        return cls(
            value=str(d.get("value") or ""),
            display_name=strip_html(str(d.get("displayName") or d.get("value") or "")),
            orderable=str(d.get("orderable", "false")).lower() == "true",
            searchable=str(d.get("searchable", "true")).lower() == "true",
            operators=[str(o) for o in d.get("operators") or []],
            types=[str(t) for t in d.get("types") or []],
            cfid=str(d.get("cfid") or ""),
        )

    @property
    def name_for_suggestions(self) -> str:
        """``fieldName=`` for the suggestions endpoint (``cf[10002]`` for custom fields)."""
        return self.cfid or self.value.strip('"')


@dataclass
class JqlAutocompleteData:
    fields: List[JqlFieldInfo] = field(default_factory=list)
    functions: List[str] = field(default_factory=list)
    reserved: List[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, d: J) -> "JqlAutocompleteData":
        return cls(
            fields=[JqlFieldInfo.from_json(x) for x in d.get("visibleFieldNames") or [] if isinstance(x, dict)],
            functions=[str(x.get("value") or "") for x in d.get("visibleFunctionNames") or [] if isinstance(x, dict)],
            reserved=[str(x) for x in d.get("jqlReservedWords") or []],
        )

    def field_named(self, name: str) -> Optional[JqlFieldInfo]:
        n = name.strip('"').lower()
        for f in self.fields:
            if f.value.strip('"').lower() == n or f.display_name.lower() == n or (f.cfid and f.cfid.lower() == n):
                return f
        return None

    @property
    def orderable_fields(self) -> List[JqlFieldInfo]:
        return [f for f in self.fields if f.orderable]


@dataclass
class Suggestion:
    value: str
    display: str

    @classmethod
    def from_json(cls, d: J) -> "Suggestion":
        value = str(d.get("value") or "")
        return cls(value=value, display=strip_html(str(d.get("displayName") or value)))
