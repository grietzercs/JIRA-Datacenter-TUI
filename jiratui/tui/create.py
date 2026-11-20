"""Issue creation: project → issue type → a form generated from createmeta.

The form shows the common fields (summary, description, assignee, priority,
labels, components, story points, epic link, sprint) when the create screen
of that project/type has them, plus every other field that is required and
has no default. Required fields are validated locally; Jira's own field
errors (the ``errors`` map of a 400 response) are shown under each field.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Footer, Header, Input, Label, Select, Static, TextArea

from ..api.errors import BadRequestError
from ..api.models import CF_EPIC_LINK, CF_SPRINT, CreateField, FieldCatalog, IssueType, Project
from .common import BaseScreen, PickerScreen, error_text, guarded, summarize, ui_timeout

if TYPE_CHECKING:  # pragma: no cover
    from .app import JiraApp

SKIP_FIELDS = {"project", "issuetype", "attachment", "issuelinks", "timetracking", "worklog"}
COMMON_SYSTEM = ["summary", "description", "assignee", "priority", "labels", "components"]
_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


def field_kind(f: CreateField, catalog: Optional[FieldCatalog]) -> str:
    """Which editor to use for a createmeta field."""
    cat = catalog or FieldCatalog()
    if f.field_id == "description" or f.system == "environment":
        return "textarea"
    if f.custom_type == CF_SPRINT or f.field_id == cat.sprint_id:
        return "sprint"
    if f.custom_type == CF_EPIC_LINK or f.field_id == cat.epic_link_id:
        return "issuekey"
    if f.type == "issuelink" or f.field_id == "parent":
        return "parent"
    if f.type == "user":
        return "user"
    if f.type == "number":
        return "number"
    if f.type == "date":
        return "date"
    if f.type == "array" and f.items == "string" and not f.allowed_values:
        return "labels"
    if f.type == "array" and f.allowed_values:
        return "multi"
    if f.allowed_values:
        return "select"
    return "text"


def choose_fields(fields: List[CreateField], catalog: Optional[FieldCatalog]) -> List[CreateField]:
    """Common fields first (in a fixed order), then other required-without-default fields."""
    cat = catalog or FieldCatalog()
    by_id = {f.field_id: f for f in fields}
    order = COMMON_SYSTEM + [x for x in (cat.story_points_id, cat.epic_link_id, cat.sprint_id) if x]
    out = [by_id[i] for i in order if i in by_id]
    for f in fields:
        if f.field_id in SKIP_FIELDS or f in out:
            continue
        if f.needs_input or f.field_id == "parent":
            out.append(f)
    return out


def safe_id(field_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", field_id)


class FieldEditor:
    """Widgets + value conversion for one createmeta field."""

    def __init__(self, field: CreateField, kind: str) -> None:
        self.field = field
        self.kind = kind
        self.sid = safe_id(field.field_id)
        self.picked: List[str] = []  # for picker-based kinds (ids/usernames)
        self.picked_labels: Dict[str, str] = {}

    @property
    def required(self) -> bool:
        return self.field.needs_input

    def widgets(self) -> List[Widget]:
        f = self.field
        star = " *" if self.required else ""
        hint = {"labels": "  (space or comma separated)", "number": "", "date": "  (YYYY-MM-DD)",
                "issuekey": "  (epic issue key, e.g. PLAT-1)", "parent": "  (parent issue key)"}.get(self.kind, "")
        out: List[Widget] = [Label(Text(f"{f.name}{star}{hint}", style="bold"), classes="c-label")]
        wid = f"c-f-{self.sid}"
        if self.kind == "textarea":
            out.append(TextArea(id=wid, classes="c-text"))
        elif self.kind == "select":
            opts = [(Text(v.label), v.id or v.label) for v in f.allowed_values]
            out.append(Select(opts, allow_blank=True, prompt=f"choose {f.name.lower()}…", id=wid))
        elif self.kind in ("multi", "user", "sprint"):
            out.append(Button(self.button_label(), id=wid, classes="c-pick"))
        elif self.kind == "number":
            out.append(Input(placeholder="number", type="number", id=wid))
        else:
            out.append(Input(placeholder=f.name, id=wid))
        out.append(Static("", id=f"c-err-{self.sid}", classes="c-err"))
        return out

    def button_label(self) -> str:
        if self.kind == "user":
            return summarize(self.picked, self.picked_labels, empty="(nobody: choose…)")
        if self.kind == "sprint":
            return summarize(self.picked, self.picked_labels, empty="(no sprint: choose…)")
        return summarize(self.picked, self.picked_labels, empty="(none: choose…)", limit=6)

    def value(self, screen: "CreateIssueScreen") -> Tuple[bool, Any]:
        """``(has_value, json_value)``; raises ``ValueError`` with a message."""
        f = self.field
        w = screen.query_one(f"#c-f-{self.sid}")
        if self.kind == "textarea":
            text = w.text.strip()  # type: ignore[attr-defined]
            return bool(text), text
        if self.kind == "select":
            v = w.value  # type: ignore[attr-defined]
            if not isinstance(v, str) or not v:
                return False, None
            av = next((a for a in f.allowed_values if (a.id or a.label) == v), None)
            if av is not None and not av.id:
                return True, {"name": av.label}
            return True, {"id": v}  # priority, option, resolution...: all accept {"id": ...}
        if self.kind == "multi":
            if not self.picked:
                return False, None
            return True, [{"id": v} for v in self.picked]
        if self.kind == "user":
            return (True, {"name": self.picked[0]}) if self.picked else (False, None)
        if self.kind == "sprint":
            return (True, int(self.picked[0])) if self.picked else (False, None)
        text = w.value.strip()  # type: ignore[attr-defined]
        if not text:
            return False, None
        if self.kind == "number":
            try:
                num = float(text)
            except ValueError:
                raise ValueError("must be a number") from None
            return True, num
        if self.kind == "labels":
            labels = [x for x in re.split(r"[\s,]+", text) if x]
            return True, labels
        if self.kind in ("issuekey", "parent"):
            key = text.upper()
            if not _ISSUE_KEY.match(key):
                raise ValueError("must be an issue key like PLAT-1")
            return True, ({"key": key} if self.kind == "parent" else key)
        if self.kind == "date":
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
                raise ValueError("use YYYY-MM-DD")
            return True, text
        if f.type == "array":
            return True, [x.strip() for x in text.split(",") if x.strip()]
        return True, text


class CreateIssueScreen(BaseScreen):
    BINDINGS = [
        Binding("ctrl+s", "create", "Create"),
        Binding("ctrl+p", "pick_project", "Project"),
        Binding("ctrl+t", "pick_type", "Issue type"),
        Binding("ctrl+o", "open_created", "Open created", show=False),
    ]

    DEFAULT_CSS = """
    CreateIssueScreen #c-status { height: auto; padding: 0 1; }
    CreateIssueScreen #c-status.error { color: $error; }
    CreateIssueScreen #c-status.ok { color: $success; }
    CreateIssueScreen #c-top { height: auto; padding: 0 1; }
    CreateIssueScreen #c-top Button { margin-right: 2; }
    CreateIssueScreen #c-form { height: 1fr; padding: 0 1; }
    CreateIssueScreen .c-label { margin-top: 1; }
    CreateIssueScreen .c-text { height: 7; }
    CreateIssueScreen .c-pick { width: 100%; }
    CreateIssueScreen .c-err { color: $error; height: auto; display: none; }
    CreateIssueScreen .c-err.show { display: block; }
    CreateIssueScreen #c-actions { height: auto; padding: 0 1; }
    CreateIssueScreen #c-actions Button { margin-right: 2; }
    """

    def __init__(self, project_key: str = "") -> None:
        super().__init__()
        self.initial_project = project_key
        self.project: Optional[Project] = None
        self.issue_type: Optional[IssueType] = None
        self.types: List[IssueType] = []
        self.editors: List[FieldEditor] = []
        self.catalog: Optional[FieldCatalog] = None
        self.created_key = ""
        self.busy = False

    @property
    def japp(self) -> "JiraApp":
        return self.app  # type: ignore[return-value]

    @property
    def client(self):
        return self.japp.require_client()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="c-status")
        with Horizontal(id="c-top"):
            yield Button("Project: (choose…)  Ctrl+P", id="c-project")
            yield Button("Issue type: (choose…)  Ctrl+T", id="c-type", disabled=True)
        yield VerticalScroll(id="c-form")
        with Horizontal(id="c-actions"):
            yield Button("Create (Ctrl+S)", variant="primary", id="c-create", disabled=True)
            yield Button("Open created issue (Ctrl+O)", id="c-open", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Create issue"
        self.sub_title = self.japp.session_label()
        self._status("Choose a project (Ctrl+P), then an issue type (Ctrl+T).")
        if self.initial_project:
            self.run_api(self._restore_project(self.initial_project), group="create-meta")
        else:
            self.action_pick_project()

    def _status(self, text: str, cls: str = "") -> None:
        st = self.query_one("#c-status", Static)
        st.update(Text(text))
        st.set_class(cls == "error", "error")
        st.set_class(cls == "ok", "ok")

    # ------------------------------------------------------------ project/type
    async def _restore_project(self, key: str) -> None:
        try:
            projects = await guarded(self.japp.cached("projects", self.client.list_projects), ui_timeout(self))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._status(f"Could not load projects: {error_text(exc)}  (Ctrl+P to retry)", "error")
            return
        match = next((p for p in projects if p.key.upper() == key.upper()), None)
        if match is None:
            self.action_pick_project()
            return
        await self._set_project(match)

    @on(Button.Pressed, "#c-project")
    def action_pick_project(self) -> None:
        client = self.client

        async def load():
            projects = await self.japp.cached("projects", client.list_projects)
            self._projects = {p.key: p for p in projects}
            return [(p.key, p.label()) for p in projects]

        def chosen(v: Optional[List[str]]) -> None:
            if v and v[0] in getattr(self, "_projects", {}):
                self.run_api(self._set_project(self._projects[v[0]]), group="create-meta")

        self.app.push_screen(PickerScreen("Create in which project?", load), chosen)

    async def _set_project(self, project: Project) -> None:
        self.project = project
        self.issue_type = None
        self.editors = []
        await self.query_one("#c-form", VerticalScroll).remove_children()
        self.query_one("#c-project", Button).label = f"Project: {project.label()}  Ctrl+P"
        self.query_one("#c-type", Button).label = "Issue type: (choose…)  Ctrl+T"
        self.query_one("#c-create", Button).disabled = True
        self._status(f"Loading issue types for {project.key}…  (Esc to cancel)")
        try:
            self.types = await guarded(self.client.createmeta_issue_types(project.key), ui_timeout(self))
        except asyncio.CancelledError:
            self._status("Cancelled. Ctrl+P to choose the project again.", "error")
            raise
        except Exception as exc:  # noqa: BLE001
            self._status(f"Could not load issue types: {error_text(exc)}\nCtrl+P to retry.", "error")
            return
        self.query_one("#c-type", Button).disabled = False
        mode = {"issuetypes": "createmeta/{project}/issuetypes (DC 8.4+)",
                "legacy": "legacy createmeta"}.get(self.client.createmeta_mode, "")
        self._status(f"{len(self.types)} issue type(s) in {project.key}" + (f" via {mode}" if mode else "") +
                     ". Choose one (Ctrl+T).")
        self.action_pick_type()

    @on(Button.Pressed, "#c-type")
    def action_pick_type(self) -> None:
        if self.project is None or not self.types:
            self.notify("Choose a project first (Ctrl+P).", severity="warning")
            return
        opts = [(t.id, t.name + ("  (sub-task)" if t.subtask else "")) for t in self.types]

        def chosen(v: Optional[List[str]]) -> None:
            if v:
                t = next((t for t in self.types if t.id == v[0]), None)
                if t is not None:
                    self.run_api(self._set_type(t), group="create-meta")

        self.app.push_screen(PickerScreen(f"Issue type in {self.project.key}", opts), chosen)

    async def _set_type(self, itype: IssueType) -> None:
        assert self.project is not None
        self.issue_type = itype
        self.query_one("#c-type", Button).label = f"Issue type: {itype.name}  Ctrl+T"
        self._status(f"Loading fields for {itype.name}…  (Esc to cancel)")
        try:
            if self.catalog is None:
                self.catalog = await guarded(self.japp.catalog(), ui_timeout(self))
            fields = await guarded(self.client.createmeta_fields(self.project.key, itype.id), ui_timeout(self))
        except asyncio.CancelledError:
            self._status("Cancelled. Ctrl+T to choose the issue type again.", "error")
            raise
        except Exception as exc:  # noqa: BLE001
            self._status(f"Could not load fields: {error_text(exc)}\nCtrl+T to retry.", "error")
            return
        await self._build_form(choose_fields(fields, self.catalog))

    async def _build_form(self, fields: List[CreateField]) -> None:
        form = self.query_one("#c-form", VerticalScroll)
        await form.remove_children()
        self.editors = [FieldEditor(f, field_kind(f, self.catalog)) for f in fields]
        widgets: List[Widget] = []
        for ed in self.editors:
            widgets.extend(ed.widgets())
        await form.mount(*widgets)
        self.query_one("#c-create", Button).disabled = False
        req = [ed.field.name for ed in self.editors if ed.required]
        self._status(f"Fill in the form; * = required ({', '.join(req) or 'none'}). Ctrl+S creates.")
        if self.editors:
            self.query_one(f"#c-f-{self.editors[0].sid}").focus(scroll_visible=False)
            form.scroll_home(animate=False)

    # ------------------------------------------------------------ pickers
    @on(Button.Pressed, ".c-pick")
    def _pick(self, ev: Button.Pressed) -> None:
        ed = next((e for e in self.editors if f"c-f-{e.sid}" == ev.button.id), None)
        if ed is None or self.project is None:
            return
        key = self.project.key
        client = self.client
        if ed.kind == "user":
            async def load():
                users = await self.japp.cached(f"people:{key}", lambda: client.assignable_users(project_key=key))
                for u in users:
                    ed.picked_labels[u.name] = u.display_name
                return [(u.name, u.label()) for u in users]
            me = self.japp.username
            pinned = [("", "(nobody / automatic)")] + ([(me, f"Me ({me})")] if me else [])
            screen = PickerScreen(f"{ed.field.name} in {key}", load, pinned=pinned)
        elif ed.kind == "sprint":
            async def load():
                boards = await self.japp.cached(f"boards:{key}", lambda: client.all_boards(key))
                out = []
                for b in boards:
                    if b.has_sprints:
                        for s in await client.board_sprints(b.id):
                            ed.picked_labels[str(s.id)] = s.name
                            out.append((str(s.id), f"{s.name}  [{s.state}]  (board {b.name})"))
                return out
            screen = PickerScreen(f"Sprint ({key}: active and future)", load, pinned=[("", "(no sprint)")])
        else:
            for av in ed.field.allowed_values:
                ed.picked_labels[av.id] = av.label
            screen = PickerScreen(ed.field.name, [(av.id, av.label) for av in ed.field.allowed_values],
                                  multi=True, selected=ed.picked)

        def chosen(v: Optional[List[str]]) -> None:
            if v is None:
                return
            ed.picked = [x for x in v if x]
            ev.button.label = ed.button_label()

        self.app.push_screen(screen, chosen)

    # ------------------------------------------------------------ create
    def _field_error(self, ed: FieldEditor, text: str) -> None:
        st = self.query_one(f"#c-err-{ed.sid}", Static)
        st.update(Text(text))
        st.set_class(bool(text), "show")

    def collect(self) -> Tuple[Dict[str, Any], List[str]]:
        assert self.project is not None and self.issue_type is not None
        payload: Dict[str, Any] = {"project": {"key": self.project.key}, "issuetype": {"id": self.issue_type.id}}
        problems: List[str] = []
        for ed in self.editors:
            self._field_error(ed, "")
            try:
                has, value = ed.value(self)
            except ValueError as exc:
                self._field_error(ed, f"{ed.field.name} {exc}")
                problems.append(ed.field.name)
                continue
            if has:
                payload[ed.field.field_id] = value
            elif ed.required:
                self._field_error(ed, f"{ed.field.name} is required.")
                problems.append(ed.field.name)
        return payload, problems

    @on(Button.Pressed, "#c-create")
    def action_create(self) -> None:
        if self.busy or self.project is None or self.issue_type is None or not self.editors:
            return
        payload, problems = self.collect()
        if problems:
            self._status(f"Please fix: {', '.join(problems)}.", "error")
            return
        self.busy = True
        self.run_api(self._create(payload), group="create")

    async def _create(self, payload: Dict[str, Any]) -> None:
        self._status("Creating issue…  (Esc stops waiting; the issue may still be created)")
        try:
            key = await guarded(self.client.create_issue(payload), ui_timeout(self))
        except asyncio.CancelledError:
            self._status("Stopped waiting. Check the project before retrying, the issue may exist.", "error")
            raise
        except BadRequestError as exc:
            unmatched = []
            for fid, msg in exc.field_errors.items():
                ed = next((e for e in self.editors if e.field.field_id == fid), None)
                if ed is not None:
                    self._field_error(ed, msg)
                else:
                    unmatched.append(f"{fid}: {msg}")
            lines = exc.error_messages + unmatched
            self._status("Jira rejected the issue" + (":\n" + "\n".join(f"  • {m}" for m in lines) if lines else
                                                      ": see the field errors below."), "error")
            return
        except Exception as exc:  # noqa: BLE001
            self._status(f"Create failed: {error_text(exc)}", "error")
            return
        finally:
            self.busy = False
        self.created_key = key
        self.japp.issues_changed = True
        btn = self.query_one("#c-open", Button)
        btn.label = f"Open {key} (Ctrl+O)"
        btn.disabled = False
        btn.focus()
        self._status(f"Created {key}  ✓   Ctrl+O / Enter to open it, or edit the form to create another.", "ok")
        self.notify(f"Created {key}")

    @on(Button.Pressed, "#c-open")
    def action_open_created(self) -> None:
        if self.created_key:
            from .issue import IssueDetailScreen

            self.app.push_screen(IssueDetailScreen(self.created_key))
