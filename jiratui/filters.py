"""Filter model shared by the Sprints and Backlog views, plus local persistence.

* :class:`CustomFilter` is a named, saved filter: simple structured parts
  (status / type / priority / text contains) plus an optional raw JQL
  fragment, all ANDed. Jira favourite filters are wrapped in the same class
  (``source="jira"``) so the UI treats both alike.
* :class:`FilterState` is what a view currently applies: labels (OR), people
  (OR, incl. Unassigned) and at most one custom filter; categories are ANDed.
* :class:`FilterStore` persists custom filters in ``filters.json`` and
  :class:`QueryStore` keeps JQL history and saved queries in ``queries.json``,
  both under the config dir (``$XDG_CONFIG_HOME/jira-tui``, mode 0600).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from . import jql
from .config import config_dir, read_json, write_json_private

GLOBAL = "global"
PROJECT = "project"


@dataclass
class CustomFilter:
    name: str
    jql: str = ""
    statuses: List[str] = field(default_factory=list)
    issue_types: List[str] = field(default_factory=list)
    priorities: List[str] = field(default_factory=list)
    text: str = ""
    scope: str = PROJECT  # project | global
    project_key: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source: str = "local"  # local | jira (favourite filter, read-only)
    jira_id: str = ""

    def fragment(self) -> str:
        """The JQL ANDed onto the view's query (ORDER BY is dropped)."""
        return jql.and_join([
            jql.in_clause("status", self.statuses),
            jql.in_clause("issuetype", self.issue_types),
            jql.in_clause("priority", self.priorities),
            jql.text_clause(self.text),
            self.jql,
        ])

    @property
    def is_empty(self) -> bool:
        return not self.fragment()

    @property
    def read_only(self) -> bool:
        return self.source == "jira"

    def scope_label(self) -> str:
        if self.source == "jira":
            return "Jira favourite"
        return "global" if self.scope == GLOBAL else f"project {self.project_key}"

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "CustomFilter":
        def strs(v: Any) -> List[str]:
            return [str(x) for x in v] if isinstance(v, list) else []

        return cls(
            name=str(d.get("name") or "Unnamed"),
            jql=str(d.get("jql") or ""),
            statuses=strs(d.get("statuses")),
            issue_types=strs(d.get("issue_types")),
            priorities=strs(d.get("priorities")),
            text=str(d.get("text") or ""),
            scope=GLOBAL if d.get("scope") == GLOBAL else PROJECT,
            project_key=str(d.get("project_key") or ""),
            id=str(d.get("id") or uuid.uuid4().hex[:12]),
            source="local",
        )

    @classmethod
    def from_jira(cls, jira_filter: Any) -> "CustomFilter":
        return cls(name=jira_filter.name, jql=jira_filter.jql, scope=GLOBAL, source="jira",
                   jira_id=jira_filter.id, id=f"jira-{jira_filter.id}")


@dataclass
class FilterState:
    labels: List[str] = field(default_factory=list)
    people: List[str] = field(default_factory=list)  # usernames and/or jql.UNASSIGNED
    custom: Optional[CustomFilter] = None

    def fragments(self) -> List[str]:
        out = [jql.labels_clause(self.labels), jql.people_clause(self.people)]
        if self.custom is not None:
            out.append(self.custom.fragment())
        return [f for f in out if f]

    def fragment(self) -> str:
        return jql.and_join(self.fragments())

    @property
    def is_empty(self) -> bool:
        return not self.fragments()

    def clear(self) -> None:
        self.labels = []
        self.people = []
        self.custom = None


class FilterStore:
    """Saved custom filters (``filters.json``)."""

    FILE = "filters.json"

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or f"{config_dir()}/{self.FILE}"
        self.filters: List[CustomFilter] = []
        self.load()

    def load(self) -> None:
        data = read_json(self.path)
        raw = data.get("filters") if isinstance(data, dict) else None
        self.filters = [CustomFilter.from_json(f) for f in raw or [] if isinstance(f, dict)]

    def save(self) -> str:
        return write_json_private(self.path, {"version": 1, "filters": [f.to_json() for f in self.filters]})

    def for_project(self, project_key: str) -> List[CustomFilter]:
        """Filters for ``project_key`` first, then global ones (each sorted by name)."""
        key = (project_key or "").upper()
        mine = [f for f in self.filters if f.scope == PROJECT and f.project_key.upper() == key]
        glob = [f for f in self.filters if f.scope == GLOBAL]
        return sorted(mine, key=lambda f: f.name.lower()) + sorted(glob, key=lambda f: f.name.lower())

    def get(self, filter_id: str) -> Optional[CustomFilter]:
        return next((f for f in self.filters if f.id == filter_id), None)

    def upsert(self, flt: CustomFilter) -> None:
        if flt.read_only:
            raise ValueError("Jira favourite filters are read-only here")
        for i, f in enumerate(self.filters):
            if f.id == flt.id:
                self.filters[i] = flt
                break
        else:
            self.filters.append(flt)
        self.save()

    def delete(self, filter_id: str) -> bool:
        before = len(self.filters)
        self.filters = [f for f in self.filters if f.id != filter_id]
        if len(self.filters) != before:
            self.save()
            return True
        return False


@dataclass
class SavedQuery:
    name: str
    jql: str


class QueryStore:
    """JQL history (most recent first, deduplicated, bounded) and saved queries."""

    FILE = "queries.json"
    MAX_HISTORY = 100

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or f"{config_dir()}/{self.FILE}"
        self.history: List[str] = []
        self.saved: List[SavedQuery] = []
        self.load()

    def load(self) -> None:
        data = read_json(self.path)
        if not isinstance(data, dict):
            return
        self.history = [str(h) for h in data.get("history") or [] if isinstance(h, str) and h.strip()]
        self.saved = [SavedQuery(str(q.get("name") or ""), str(q.get("jql") or ""))
                      for q in data.get("saved") or [] if isinstance(q, dict) and q.get("jql")]

    def save(self) -> str:
        return write_json_private(self.path, {
            "version": 1,
            "updated": int(time.time()),
            "history": self.history[: self.MAX_HISTORY],
            "saved": [asdict(q) for q in self.saved],
        })

    def add_history(self, query: str) -> None:
        q = query.strip()
        if not q:
            return
        self.history = [q] + [h for h in self.history if h != q]
        self.history = self.history[: self.MAX_HISTORY]
        self.save()

    def save_query(self, name: str, query: str) -> None:
        name = name.strip() or query.strip()[:40]
        self.saved = [s for s in self.saved if s.name != name] + [SavedQuery(name, query.strip())]
        self.saved.sort(key=lambda s: s.name.lower())
        self.save()

    def delete_query(self, name: str) -> None:
        self.saved = [s for s in self.saved if s.name != name]
        self.save()
