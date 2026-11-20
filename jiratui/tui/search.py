"""Issues view: JQL-first search across all projects and issue types.

* JQL input with completion of field names, operators, keywords, functions
  and values (``/rest/api/2/jql/autocompletedata`` and ``.../suggestions``).
  The best completion is shown inline (accept with →); more candidates are
  listed under the input.
* Jira's ``errorMessages`` for invalid JQL are shown in full.
* Persisted history (Ctrl+R) and saved queries (Ctrl+O / Ctrl+S).
* Quick-insert helpers (Ctrl+T), ORDER BY picker (Ctrl+B) and an issue-type
  filter chip (Ctrl+Y) covering every type from ``/rest/api/2/issuetype``.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Sequence, Tuple

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.suggester import Suggester
from textual.widgets import Input, Static

from .. import jql
from ..api.errors import JqlError
from ..api.models import FieldCatalog, Issue, JqlAutocompleteData, Page
from .common import PickerScreen, TextPromptScreen, error_text, summarize
from .issuetable import SEARCH_COLUMNS, IssueTable, search_row

if TYPE_CHECKING:  # pragma: no cover
    from .app import JiraApp

DEFAULT_JQL = "assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC"
KEYWORDS_AFTER_VALUE = ["AND", "OR", "ORDER BY"]
KEYWORDS_AFTER_ORDER_FIELD = ["ASC", "DESC"]
DEFAULT_OPERATORS = ["=", "!=", "in", "not in", "~", "is", "is not", ">", ">=", "<", "<="]
FALLBACK_ORDER_FIELDS = ["created", "updated", "priority", "key", "status", "assignee", "Rank", "duedate"]
_TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"?|\'(?:\\.|[^\'\\])*\'?|!=|!~|>=|<=|[=~<>(),]|[^\s=!~<>(),"\']+')
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_SUGGEST_TIMEOUT = 5.0


def tokenize_partial(text: str) -> Tuple[List[str], str, int]:
    """Split ``text`` into complete tokens and the trailing partial token.

    Returns ``(tokens, partial, partial_start)``. If the text ends with
    whitespace (or a delimiter) the partial token is empty.
    """
    toks = [(m.group(0), m.start()) for m in _TOKEN_RE.finditer(text)]
    if not toks:
        return [], "", len(text)
    last, start = toks[-1]
    ends_clean = text.endswith((" ", "\t")) or last in ("(", ")", ",", "=", "~", "<", ">", "!=", "!~", ">=", "<=")
    if ends_clean:
        return [t for t, _ in toks], "", len(text)
    return [t for t, _ in toks[:-1]], last, start


def jql_literal(value: str) -> str:
    """Bare value if safe, else a quoted JQL string."""
    if _SAFE_VALUE.match(value) and value.lower() not in ("and", "or", "not", "empty", "null", "order", "in", "is"):
        return value
    return jql.quote_value(value)


class JqlAssist:
    """Context-aware JQL completion backed by Jira's autocomplete endpoints."""

    def __init__(self, app: "JiraApp") -> None:
        self.app = app
        self._values: Dict[Tuple[str, str], List[str]] = {}
        self.data: Optional[JqlAutocompleteData] = None
        self.last_error = ""

    async def _data(self) -> JqlAutocompleteData:
        if self.data is None:
            self.data = await asyncio.wait_for(self.app.require_client().jql_autocomplete(), _SUGGEST_TIMEOUT)
        return self.data

    async def _value_suggestions(self, field: str, prefix: str) -> List[str]:
        data = await self._data()
        info = data.field_named(field)
        name = info.name_for_suggestions if info else field.strip('"')
        key = (name.lower(), prefix.lower())
        if key not in self._values:
            sugg = await asyncio.wait_for(self.app.require_client().jql_suggestions(name, prefix), _SUGGEST_TIMEOUT)
            self._values[key] = [s.value for s in sugg]
            if len(self._values) > 500:
                self._values.pop(next(iter(self._values)))
        return self._values[key]

    async def candidates(self, text: str) -> Tuple[int, str, List[str]]:
        """``(partial_start, partial, candidates)`` for completing ``text`` at its end."""
        tokens, partial, start = tokenize_partial(text)
        low = [t.lower() for t in tokens]
        data = await self._data()
        field_names = [f.value for f in data.fields if f.searchable]
        functions = data.functions

        # Position after ORDER BY?
        if "order" in low and "by" in low[low.index("order"):]:
            after = tokens[low.index("by", low.index("order")) + 1:]
            if not after or after[-1] == ",":
                pool = [f.value for f in data.orderable_fields] or FALLBACK_ORDER_FIELDS
            elif after[-1].lower() in ("asc", "desc"):
                pool = [","]
            else:
                pool = KEYWORDS_AFTER_ORDER_FIELD + [","]
            return start, partial, _match(pool, partial)

        prev = low[-1] if low else ""
        prev2 = low[-2] if len(low) > 1 else ""
        # value position: after an operator, or inside an "in (" list
        in_list_field = _list_field(tokens)
        if in_list_field is None and (not tokens or prev in ("and", "or", "(")
                                      or (prev == "not" and prev2 in ("and", "or", "(", ""))):
            return start, partial, _match(field_names, partial)
        if prev in ("=", "!=", "~", "!~", ">", ">=", "<", "<=") or in_list_field is not None or \
                (prev in ("is",) or (prev == "not" and prev2 == "is")):
            if prev == "is" or (prev == "not" and prev2 == "is"):
                return start, partial, _match(["EMPTY", "NULL", "not"] if prev == "is" else ["EMPTY", "NULL"],
                                              partial)
            field = in_list_field if in_list_field is not None else _field_before_operator(tokens)
            raw_prefix = partial.lstrip('"\'')
            try:
                values = await self._value_suggestions(field, raw_prefix)
                self.last_error = ""
            except Exception as exc:  # noqa: BLE001 - completion must never break typing
                self.last_error = error_text(exc)
                values = []
            pool = [jql_literal(v) for v in values] + [f for f in functions]
            if in_list_field is None and prev in ("=", "!="):
                pool.append("EMPTY")
            return start, partial, _match(pool, partial, contains=True)
        if prev in ("in",) and prev2 == "not":
            return start, partial, _match(["("], partial)
        if prev == "in":
            return start, partial, _match(["("] + [f for f in functions if f.lower().endswith("sprints()")], partial)
        # after a field name: operators
        info = data.field_named(tokens[-1]) if tokens else None
        if info is not None and prev not in (")",) and (len(low) < 2 or low[-2] in ("and", "or", "not", "(")):
            ops = info.operators or DEFAULT_OPERATORS
            return start, partial, _match(ops, partial)
        if prev == "not" and prev2 not in ("and", "or", ""):
            return start, partial, _match(["in"], partial)
        # after a complete value: keywords
        return start, partial, _match(KEYWORDS_AFTER_VALUE, partial)


def _list_field(tokens: Sequence[str]) -> Optional[str]:
    """If the cursor is inside ``field in (a, b, |``, return ``field``."""
    depth = 0
    for i in range(len(tokens) - 1, -1, -1):
        t = tokens[i]
        if t == ")":
            depth += 1
        elif t == "(":
            if depth == 0:
                if i >= 2 and tokens[i - 1].lower() == "in":
                    j = i - 2
                    if tokens[j].lower() == "not":
                        j -= 1
                    return tokens[j] if j >= 0 else None
                return None
            depth -= 1
    return None


def _field_before_operator(tokens: Sequence[str]) -> str:
    return tokens[-2] if len(tokens) >= 2 else ""


def _match(pool: Sequence[str], partial: str, contains: bool = False) -> List[str]:
    p = partial.lower()
    starts = [c for c in pool if c.lower().startswith(p) and c.lower() != p]
    if contains and p:
        bare = p.strip("\"'")
        starts += [c for c in pool if c not in starts and bare and bare in c.lower() and c.lower() != p]
    seen: List[str] = []
    for c in starts:
        if c not in seen:
            seen.append(c)
    return seen[:20]


class JqlSuggester(Suggester):
    """Inline completion for the JQL input. Also reports the candidate list."""

    def __init__(self, assist: JqlAssist, on_candidates: Callable[[List[str], str], None]) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self.assist = assist
        self.on_candidates = on_candidates

    async def get_suggestion(self, value: str) -> Optional[str]:
        if not value.strip():
            self.on_candidates([], "")
            return None
        try:
            start, partial, cands = await self.assist.candidates(value)
        except Exception as exc:  # noqa: BLE001
            self.on_candidates([], f"autocomplete unavailable: {error_text(exc)}")
            return None
        self.on_candidates(cands, self.assist.last_error)
        for c in cands:
            if c.lower().startswith(partial.lower()):
                return value + c[len(partial):]
        return None


class IssuesPane(Vertical):
    BINDINGS = [
        Binding("ctrl+r", "history", "History"),
        Binding("ctrl+o", "saved", "Saved"),
        Binding("ctrl+s", "save_query", "Save query"),
        Binding("ctrl+t", "insert", "Insert"),
        Binding("ctrl+b", "order_by", "Order by"),
        Binding("ctrl+y", "types", "Types"),
        Binding("ctrl+l", "focus_jql", "JQL", show=False),
    ]

    DEFAULT_CSS = """
    IssuesPane { height: 1fr; }
    IssuesPane #jql { margin: 0; }
    IssuesPane #jql-suggest { height: auto; max-height: 3; color: $text-muted; padding: 0 1; }
    IssuesPane #jql-chips { height: auto; padding: 0 1; background: $boost; }
    IssuesPane #jql-error { height: auto; color: $error; padding: 0 1; }
    """

    view_name = "search"

    def __init__(self, id: Optional[str] = None) -> None:
        super().__init__(id=id)
        self.types: List[str] = []
        self.query_text = ""
        self.catalog: Optional[FieldCatalog] = None
        self._activated = False
        self.assist: Optional[JqlAssist] = None

    @property
    def japp(self) -> "JiraApp":
        return self.app  # type: ignore[return-value]

    @property
    def client(self):
        return self.japp.require_client()

    def compose(self) -> ComposeResult:
        self.assist = JqlAssist(self.app)  # type: ignore[arg-type]
        yield Static(Text("JQL across all projects · Enter run · → accept completion · Ctrl+R history · "
                          "Ctrl+O saved · Ctrl+S save · Ctrl+T insert · Ctrl+B order by · Ctrl+Y types",
                          style="dim"), id="jql-help")
        yield Input(placeholder="JQL, e.g. project = PLAT AND status != Done ORDER BY updated DESC", id="jql",
                    suggester=JqlSuggester(self.assist, self._show_candidates))
        yield Static("", id="jql-suggest")
        yield Static("", id="jql-chips")
        yield Static("", id="jql-error")
        yield IssueTable(SEARCH_COLUMNS, self._fetch, search_row, limit=50, autoload=False,
                         empty_text="No issues match.", noun="issue", id="search-issues")

    @property
    def table(self) -> IssueTable:
        return self.query_one(IssueTable)

    @property
    def jql_input(self) -> Input:
        return self.query_one("#jql", Input)

    def on_mount(self) -> None:
        self._update_chips()
        self.table.show_message("Type a JQL query and press Enter.")

    def activate(self) -> None:
        if not self._activated:
            self._activated = True
            initial = self.japp.query_store.history[0] if self.japp.query_store.history else DEFAULT_JQL
            self.jql_input.value = initial
            self.run_query(initial)
        self.jql_input.focus()

    def focus_main(self) -> None:
        self.jql_input.focus()

    def action_focus_jql(self) -> None:
        self.jql_input.focus()

    def _show_candidates(self, cands: List[str], problem: str) -> None:
        t = Text()
        if cands:
            t.append("Suggestions: ", style="bold")
            t.append("  ".join(cands[:10]))
            if len(cands) > 10:
                t.append(f"  (+{len(cands) - 10})", style="dim")
        if problem:
            t.append(("\n" if cands else "") + problem, style="yellow")
        self.query_one("#jql-suggest", Static).update(t)

    def effective_jql(self) -> str:
        return jql.compose(self.query_text, [jql.in_clause("issuetype", self.types)])

    def _update_chips(self) -> None:
        t = Text()
        t.append("Types: ", style="bold")
        t.append(summarize(self.types, empty="all types", limit=5), style="cyan" if self.types else "dim")
        t.append(" [Ctrl+Y]   ", style="dim")
        _, order = jql.split_order_by(self.query_text)
        t.append("Order: ", style="bold")
        t.append(order or "Jira default", style="cyan" if order else "dim")
        t.append(" [Ctrl+B]\n", style="dim")
        t.append("Effective JQL: ", style="bold")
        t.append(self.effective_jql() or "(empty: all issues you can see)", style="green")
        self.query_one("#jql-chips", Static).update(t)

    def _set_error(self, exc: Optional[BaseException]) -> None:
        st = self.query_one("#jql-error", Static)
        if exc is None:
            st.update("")
            return
        if isinstance(exc, JqlError):
            t = Text("JQL error:\n", style="bold")
            for m in exc.error_messages or [exc.message]:
                t.append(f"  • {m}\n")
            st.update(t)
        else:
            st.update(Text(error_text(exc)))

    def run_query(self, text: str) -> None:
        self.query_text = text.strip()
        self._set_error(None)
        self._update_chips()
        self.table.reload()

    async def _fetch(self, start: int, limit: int, flt: str) -> Page[Issue]:
        if self.catalog is None:
            self.catalog = await self.japp.catalog()
        q = self.effective_jql()
        try:
            page = await self.client.search(q, start, limit, fields=self.catalog.issue_fields(), catalog=self.catalog)
        except Exception as exc:
            self._set_error(exc)
            raise
        if start == 0 and self.query_text:
            self.japp.query_store.add_history(self.query_text)
        return page

    @on(Input.Submitted, "#jql")
    def _submitted(self, ev: Input.Submitted) -> None:
        ev.stop()
        self.run_query(ev.value)
        self.table.focus_table()

    def _set_and_run(self, text: str) -> None:
        self.jql_input.value = text
        self.jql_input.cursor_position = len(text)
        self.run_query(text)

    # ------------------------------------------------------------ actions
    def action_history(self) -> None:
        hist = self.japp.query_store.history
        if not hist:
            self.notify("No query history yet.", timeout=2)
            return

        def chosen(v: Optional[List[str]]) -> None:
            if v:
                self._set_and_run(v[0])

        self.app.push_screen(PickerScreen("JQL history (most recent first)", [(h, h) for h in hist]), chosen)

    def action_saved(self) -> None:
        saved = self.japp.query_store.saved
        delete = "__delete__"
        opts = [(s.jql, f"{s.name}  —  {s.jql}") for s in saved]
        if saved:
            opts.append((delete, "✗ Delete a saved query…"))
        if not opts:
            self.notify("No saved queries yet: Ctrl+S saves the current one.", timeout=3)
            return

        def chosen(v: Optional[List[str]]) -> None:
            if not v:
                return
            if v[0] == delete:
                self.app.push_screen(PickerScreen("Delete which saved query?", [(s.name, s.name) for s in saved]),
                                     lambda d: self._delete_saved(d))
                return
            self._set_and_run(v[0])

        self.app.push_screen(PickerScreen("Saved queries", opts), chosen)

    def _delete_saved(self, names: Optional[List[str]]) -> None:
        if names:
            self.japp.query_store.delete_query(names[0])
            self.notify(f"Deleted saved query '{names[0]}'.")

    def action_save_query(self) -> None:
        text = self.jql_input.value.strip()
        if not text:
            self.notify("Nothing to save.", severity="warning")
            return

        def named(name: Optional[str]) -> None:
            if name:
                self.japp.query_store.save_query(name, text)
                self.notify(f"Saved query '{name.strip()}'.")

        self.app.push_screen(TextPromptScreen("Name for this saved query", multiline=False,
                                              placeholder="e.g. My open bugs"), named)

    def _insert(self, clause: str) -> None:
        current = self.jql_input.value
        new = jql.compose(current, [clause])
        self.jql_input.value = new
        self.jql_input.cursor_position = len(new)
        self.jql_input.focus()

    def action_insert(self) -> None:
        client = self.client
        helpers = [
            ("project", "project in (…)  pick projects"),
            ("issuetype", "issuetype in (…)  pick issue types"),
            ("status", "status in (…)  pick statuses"),
            ("me", "assignee = currentUser()"),
            ("assignee", "assignee in (…)  pick people (by project)"),
            ("unresolved", "resolution = Unresolved"),
            ("open-sprints", "sprint in openSprints()"),
            ("recent", "updated >= -7d"),
        ]

        def chosen(v: Optional[List[str]]) -> None:
            if not v:
                return
            what = v[0]
            fixed = {"me": "assignee = currentUser()", "unresolved": "resolution = Unresolved",
                     "open-sprints": "sprint in openSprints()", "recent": "updated >= -7d"}
            if what in fixed:
                self._insert(fixed[what])
            elif what == "project":
                async def load():
                    ps = await self.japp.cached("projects", client.list_projects)
                    return [(p.key, p.label()) for p in ps]
                self._pick_into("project", "Projects", load)
            elif what == "issuetype":
                async def load():
                    ts = await self.japp.cached("issuetypes", client.issue_types)
                    return [(t.name, t.name + ("  (sub-task)" if t.subtask else "")) for t in ts]
                self._pick_into("issuetype", "Issue types", load)
            elif what == "status":
                async def load():
                    ss = await self.japp.cached("statuses:", lambda: client.statuses(""))
                    return [(s.name, s.name) for s in ss]
                self._pick_into("status", "Statuses", load)
            elif what == "assignee":
                self._pick_assignee()

        self.app.push_screen(PickerScreen("Insert into the query", helpers), chosen)

    def _pick_into(self, field: str, title: str, load) -> None:
        def chosen(v: Optional[List[str]]) -> None:
            if v:
                self._insert(jql.in_clause(field, v) if len(v) > 1 else f"{field} = {jql.quote_value(v[0])}")

        self.app.push_screen(PickerScreen(title, load, multi=True), chosen)

    def _pick_assignee(self) -> None:
        client = self.client

        async def load_projects():
            ps = await self.japp.cached("projects", client.list_projects)
            return [(p.key, p.label()) for p in ps]

        def project_chosen(v: Optional[List[str]]) -> None:
            if not v:
                return
            key = v[0]

            async def load_people():
                users = await self.japp.cached(f"people:{key}", lambda: client.assignable_users(project_key=key))
                return [(u.name, u.label()) for u in users]

            def people_chosen(names: Optional[List[str]]) -> None:
                if names:
                    self._insert(jql.people_clause(names))

            self.app.push_screen(PickerScreen(f"Assignable people in {key}", load_people, multi=True,
                                              pinned=[(jql.UNASSIGNED, "Unassigned (assignee is EMPTY)")]),
                                 people_chosen)

        self.app.push_screen(PickerScreen("People from which project?", load_projects), project_chosen)

    def action_order_by(self) -> None:
        client = self.client

        async def load():
            try:
                data = await self.japp.cached("jql-autocomplete", client.jql_autocomplete)
                fields = [f.value.strip('"') for f in data.orderable_fields]
            except Exception:  # noqa: BLE001 - fall back to common sortable fields
                fields = []
            return [(f, f) for f in (fields or FALLBACK_ORDER_FIELDS)]

        def field_chosen(v: Optional[List[str]]) -> None:
            if not v:
                return
            field = v[0]

            def dir_chosen(d: Optional[List[str]]) -> None:
                if d:
                    self._set_and_run(jql.set_order_by(self.jql_input.value, field, d[0]))

            self.app.push_screen(PickerScreen(f"Order by {field}", [("DESC", "Descending (DESC)"),
                                                                   ("ASC", "Ascending (ASC)")]), dir_chosen)

        self.app.push_screen(PickerScreen("Order by which field?", load), field_chosen)

    def action_types(self) -> None:
        client = self.client

        async def load():
            ts = await self.japp.cached("issuetypes", client.issue_types)
            return [(t.name, t.name + ("  (sub-task)" if t.subtask else "")) for t in ts]

        def chosen(v: Optional[List[str]]) -> None:
            if v is not None:
                self.types = v
                self.run_query(self.jql_input.value)

        self.app.push_screen(PickerScreen("Issue types (any of; none = all types)", load, multi=True,
                                          selected=self.types), chosen)
