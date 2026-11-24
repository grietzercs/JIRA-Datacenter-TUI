"""Filter UI shared by the Sprints and Backlog views.

* :class:`FilterBar` shows the active label / people / custom filters and the
  effective JQL.
* :class:`CustomFiltersScreen` lists saved custom filters (this project's,
  then global ones) plus the user's Jira favourite filters; apply, create,
  edit, delete.
* :class:`FilterEditorScreen` edits one custom filter: structured parts
  (status, type, priority, text contains) plus a raw JQL fragment, validated
  against the server before saving.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, RadioButton, RadioSet, Static
from textual.widgets.option_list import Option
from textual.worker import Worker

from .. import jql
from ..api.errors import JqlError
from ..filters import GLOBAL, PROJECT, CustomFilter, FilterState
from .common import ConfirmScreen, PickerScreen, error_text, guarded, summarize, ui_timeout

if TYPE_CHECKING:  # pragma: no cover
    from .app import JiraApp

FilterResult = Tuple[str, Optional[CustomFilter]]  # ("apply"|"clear", filter)


class FilterBar(Static):
    """Two-line summary of what is filtered and the JQL actually sent."""

    DEFAULT_CSS = """
    FilterBar { height: auto; padding: 0 1; background: $boost; }
    """

    def show(self, state: FilterState, effective: str, people_labels: Optional[Dict[str, str]] = None,
             note: str = "") -> None:
        t = Text()
        labels = {jql.UNASSIGNED: "Unassigned", **(people_labels or {})}
        t.append("Labels: ", style="bold")
        t.append(summarize(state.labels), style="cyan" if state.labels else "dim")
        t.append(" [l]   ", style="dim")
        t.append("People: ", style="bold")
        t.append(summarize(state.people, labels), style="cyan" if state.people else "dim")
        t.append(" [a]   ", style="dim")
        t.append("Custom: ", style="bold")
        if state.custom is not None:
            t.append(state.custom.name, style="cyan")
            t.append(f" ({state.custom.scope_label()})", style="dim")
        else:
            t.append("none", style="dim")
        t.append(" [f, F new]   X clear all\n", style="dim")
        t.append("JQL: ", style="bold")
        t.append(effective or "(nothing selected)", style="green" if effective else "dim")
        if note:
            t.append(f"\n{note}", style="yellow")
        self.update(t)


# ------------------------------------------------------------------ editor
class FilterEditorScreen(ModalScreen[Optional[CustomFilter]]):
    DEFAULT_CSS = """
    FilterEditorScreen { align: center middle; }
    FilterEditorScreen > Vertical { width: 100; max-width: 100%; height: 90%; border: thick $primary; background: $surface; padding: 0 1; }
    FilterEditorScreen VerticalScroll { height: 1fr; }
    FilterEditorScreen Label.field { margin-top: 1; text-style: bold; width: 100%; }
    FilterEditorScreen .pick { width: 100%; }
    FilterEditorScreen #fe-preview { height: auto; color: $success; margin-top: 1; }
    FilterEditorScreen #fe-error { height: auto; color: $error; }
    FilterEditorScreen #fe-error.ok { color: $success; }
    FilterEditorScreen Horizontal { height: auto; margin-top: 1; }
    FilterEditorScreen Horizontal Button { margin-right: 2; }
    FilterEditorScreen RadioSet { width: 100%; }
    """
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+t", "test", "Test JQL"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, project_key: str, flt: Optional[CustomFilter] = None) -> None:
        super().__init__()
        self.project_key = project_key
        self.original = flt
        base = flt or CustomFilter(name="", project_key=project_key, scope=PROJECT if project_key else GLOBAL)
        self.flt = CustomFilter.from_json(base.to_json())  # edit a copy
        self._worker: Optional[Worker] = None

    @property
    def japp(self) -> "JiraApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        f = self.flt
        title = "Edit custom filter" if self.original else "New custom filter"
        with Vertical():
            yield Static(Text(f"{title}  (Ctrl+S save · Ctrl+T test · Esc cancel)", style="bold"))
            with VerticalScroll():
                yield Label("Name", classes="field")
                yield Input(f.name, placeholder="e.g. Open high-priority work", id="fe-name")
                yield Label("Scope", classes="field")
                with RadioSet(id="fe-scope"):
                    yield RadioButton(f"This project ({self.project_key or 'none selected'})",
                                      value=f.scope == PROJECT, id="fe-scope-project", disabled=not self.project_key)
                    yield RadioButton("Global (all projects)", value=f.scope == GLOBAL, id="fe-scope-global")
                yield Label("Status is any of", classes="field")
                yield Button("", id="fe-status", classes="pick")
                yield Label("Issue type is any of", classes="field")
                yield Button("", id="fe-type", classes="pick")
                yield Label("Priority is any of", classes="field")
                yield Button("", id="fe-priority", classes="pick")
                yield Label("Text contains (summary, description, comments)", classes="field")
                yield Input(f.text, placeholder="words to search for", id="fe-text")
                yield Label("Extra JQL (ANDed; e.g. priority = High AND status != Done)", classes="field")
                yield Input(f.jql, placeholder='e.g. updated >= -14d AND component = "API"', id="fe-jql")
                yield Static("", id="fe-preview")
                yield Static("", id="fe-error")
            with Horizontal():
                yield Button("Save (Ctrl+S)", variant="primary", id="fe-save")
                yield Button("Test JQL (Ctrl+T)", id="fe-test")
                yield Button("Cancel", id="fe-cancel")

    def on_mount(self) -> None:
        self._refresh()
        self.query_one("#fe-name", Input).focus()

    def _collect(self) -> CustomFilter:
        f = self.flt
        f.name = self.query_one("#fe-name", Input).value.strip()
        f.text = self.query_one("#fe-text", Input).value.strip()
        f.jql = self.query_one("#fe-jql", Input).value.strip()
        f.scope = GLOBAL if self.query_one("#fe-scope-global", RadioButton).value or not self.project_key else PROJECT
        f.project_key = self.project_key if f.scope == PROJECT else ""
        return f

    def _refresh(self) -> None:
        f = self._collect()
        self.query_one("#fe-status", Button).label = summarize(f.statuses, empty="any status (choose…)", limit=6)
        self.query_one("#fe-type", Button).label = summarize(f.issue_types, empty="any type (choose…)", limit=6)
        self.query_one("#fe-priority", Button).label = summarize(f.priorities, empty="any priority (choose…)",
                                                                 limit=6)
        frag = f.fragment()
        self.query_one("#fe-preview", Static).update(Text(f"JQL fragment: {frag}" if frag else
                                                          "JQL fragment: (empty: choose something above)"))

    def _error(self, text: str, ok: bool = False) -> None:
        st = self.query_one("#fe-error", Static)
        st.update(Text(text))
        st.set_class(ok, "ok")

    @on(Input.Changed)
    def _changed(self) -> None:
        self._refresh()

    @on(RadioSet.Changed)
    def _scope_changed(self) -> None:
        self._refresh()

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self.action_save()

    @on(Button.Pressed, ".pick")
    def _pick(self, ev: Button.Pressed) -> None:
        client = self.japp.require_client()
        key = self.project_key
        if ev.button.id == "fe-status":
            async def load():
                sts = await self.japp.cached(f"statuses:{key}", lambda: client.statuses(key))
                return [(s.name, f"{s.name}  ({s.category_name})" if s.category_name else s.name) for s in sts]
            title, attr = "Status is any of", "statuses"
        elif ev.button.id == "fe-type":
            async def load():
                ts = await self.japp.cached("issuetypes", client.issue_types)
                return [(t.name, t.name + ("  (sub-task)" if t.subtask else "")) for t in ts]
            title, attr = "Issue type is any of", "issue_types"
        else:
            async def load():
                ps = await self.japp.cached("priorities", client.priorities)
                return [(p.name, p.name) for p in ps]
            title, attr = "Priority is any of", "priorities"

        def done(values: Optional[List[str]]) -> None:
            if values is not None:
                setattr(self.flt, attr, values)
                self._refresh()

        self.app.push_screen(PickerScreen(title, load, multi=True, selected=getattr(self.flt, attr)), done)

    def _validation_query(self, f: CustomFilter) -> str:
        base = jql.project_clause(self.project_key) if f.scope == PROJECT and self.project_key else ""
        return jql.and_join([base, f.fragment()])

    def action_test(self) -> None:
        f = self._collect()
        if f.is_empty:
            self._error("The filter is empty: choose a status/type/priority, enter text or JQL.")
            return
        self._run(self._validate(f, save=False))

    def action_save(self) -> None:
        f = self._collect()
        if not f.name:
            self._error("A name is required.")
            self.query_one("#fe-name", Input).focus()
            return
        if f.is_empty:
            self._error("The filter is empty: choose a status/type/priority, enter text or JQL.")
            return
        self._run(self._validate(f, save=True))

    def _run(self, coro) -> None:
        if self._worker is not None and self._worker.is_running:
            return
        self._worker = self.run_worker(coro, exclusive=True, exit_on_error=False)

    async def _validate(self, f: CustomFilter, save: bool) -> None:
        q = self._validation_query(f)
        self._error(f"Checking JQL with the server…  {q}")
        try:
            total = await guarded(self.japp.require_client().validate_jql(q), ui_timeout(self))
        except asyncio.CancelledError:
            raise
        except JqlError as exc:
            self._error("Jira rejected the JQL:\n" + "\n".join(f"  • {m}" for m in exc.error_messages or [exc.message]))
            return
        except Exception as exc:  # noqa: BLE001 - offline etc.: allow saving, but say so
            if save:
                self.notify(f"Saved without server validation: {error_text(exc)}", severity="warning", timeout=6)
                self.dismiss(f)
            else:
                self._error(f"Could not validate: {error_text(exc)}")
            return
        if save:
            self.dismiss(f)
        else:
            self._error(f"JQL is valid: {total} matching issue(s).", ok=True)

    @on(Button.Pressed, "#fe-save")
    def _save_pressed(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#fe-test")
    def _test_pressed(self) -> None:
        self.action_test()

    @on(Button.Pressed, "#fe-cancel")
    def action_cancel(self) -> None:
        if self._worker is not None and self._worker.is_running:
            self._worker.cancel()
        self.dismiss(None)


# ----------------------------------------------------------------- manager
class CustomFiltersScreen(ModalScreen[Optional[FilterResult]]):
    DEFAULT_CSS = """
    CustomFiltersScreen { align: center middle; }
    CustomFiltersScreen > Vertical { width: 110; max-width: 100%; height: 85%; border: thick $primary; background: $surface; padding: 0 1; }
    CustomFiltersScreen #cf-list { height: 1fr; }
    CustomFiltersScreen #cf-status { height: auto; color: $text-muted; }
    CustomFiltersScreen #cf-status.error { color: $error; }
    """
    BINDINGS = [
        Binding("n", "new", "New"),
        Binding("e", "edit", "Edit"),
        Binding("d", "delete", "Delete"),
        Binding("x", "clear", "No filter"),
        Binding("ctrl+r", "reload_favourites", "Retry favourites", show=False),
        Binding("escape", "close", "Close"),
    ]

    def __init__(self, project_key: str, active_id: str = "") -> None:
        super().__init__()
        self.project_key = project_key
        self.active_id = active_id
        self.entries: List[CustomFilter] = []
        self.favourites: List[CustomFilter] = []
        self.fav_error = ""
        self.fav_loading = False

    @property
    def japp(self) -> "JiraApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(f"Custom filters for {self.project_key or 'all projects'}  ·  ANDed onto the view's "
                              "query", style="bold"))
            yield OptionList(id="cf-list")
            yield Static("", id="cf-status")
            yield Static(Text("Enter apply · n new · e edit · d delete · x no custom filter · Esc close   "
                              f"(saved in {self.japp.filter_store.path})", style="dim"))

    def on_mount(self) -> None:
        self._populate()
        self.query_one("#cf-list", OptionList).focus()
        self.action_reload_favourites()

    def _populate(self) -> None:
        local = self.japp.filter_store.for_project(self.project_key)
        self.entries = local + self.favourites
        ol = self.query_one("#cf-list", OptionList)
        prev = ol.highlighted
        ol.clear_options()
        if not self.entries:
            ol.add_option(Option(Text("No saved filters yet: press n to create one.", style="dim"), id="none",
                                 disabled=True))
        for i, f in enumerate(self.entries):
            t = Text()
            t.append("● " if f.id == self.active_id else "  ", style="green")
            t.append(f.name, style="bold")
            t.append(f"  [{f.scope_label()}]", style="magenta" if f.read_only else "cyan")
            t.append(f"\n    {f.fragment() or '(empty)'}", style="dim")
            ol.add_option(Option(t, id=f"f{i}"))
        if self.entries:
            ol.highlighted = min(prev or 0, len(self.entries) - 1)

    def _status(self, text: str, error: bool = False) -> None:
        st = self.query_one("#cf-status", Static)
        st.update(Text(text))
        st.set_class(error, "error")

    def action_reload_favourites(self) -> None:
        if self.fav_loading:
            return
        self.fav_loading = True
        self.run_worker(self._load_favourites(), exclusive=True, exit_on_error=False)

    async def _load_favourites(self) -> None:
        self._status("Loading your Jira favourite filters…  (/rest/api/2/filter/favourite)")
        client = self.japp.require_client()
        try:
            favs = await guarded(self.japp.cached("favourites", client.favourite_filters), ui_timeout(self))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - local filters still work
            self._status(f"Jira favourite filters unavailable: {error_text(exc)}  (Ctrl+R to retry)", True)
            return
        finally:
            self.fav_loading = False
        self.favourites = [CustomFilter.from_jira(f) for f in favs if f.jql]
        self._status(f"{len(self.favourites)} Jira favourite filter(s) listed (read-only).")
        self._populate()

    def _current(self) -> Optional[CustomFilter]:
        idx = self.query_one("#cf-list", OptionList).highlighted
        if idx is None or not (0 <= idx < len(self.entries)):
            return None
        return self.entries[idx]

    @on(OptionList.OptionSelected, "#cf-list")
    def _apply(self, ev: OptionList.OptionSelected) -> None:
        ev.stop()
        if 0 <= ev.option_index < len(self.entries):
            self.dismiss(("apply", self.entries[ev.option_index]))

    def action_new(self) -> None:
        def saved(f: Optional[CustomFilter]) -> None:
            if f is not None:
                self.japp.filter_store.upsert(f)
                self.notify(f"Saved filter '{f.name}'.")
                self._populate()

        self.app.push_screen(FilterEditorScreen(self.project_key), saved)

    def action_edit(self) -> None:
        cur = self._current()
        if cur is None:
            return
        if cur.read_only:
            self.notify("Jira favourite filters are edited in Jira. Press n to copy its JQL into a new local "
                        "filter instead.", severity="warning")
            return

        def saved(f: Optional[CustomFilter]) -> None:
            if f is not None:
                self.japp.filter_store.upsert(f)
                self.notify(f"Updated filter '{f.name}'.")
                self._populate()

        self.app.push_screen(FilterEditorScreen(self.project_key, cur), saved)

    def action_delete(self) -> None:
        cur = self._current()
        if cur is None:
            return
        if cur.read_only:
            self.notify("Jira favourite filters cannot be deleted from here.", severity="warning")
            return

        def confirmed(ok: Optional[bool]) -> None:
            if ok:
                self.japp.filter_store.delete(cur.id)
                self.notify(f"Deleted filter '{cur.name}'.")
                self._populate()

        self.app.push_screen(ConfirmScreen(f"Delete custom filter '{cur.name}'?", "Delete", danger=True), confirmed)

    def action_clear(self) -> None:
        self.dismiss(("clear", None))

    def action_close(self) -> None:
        self.dismiss(None)


def pinned_for(values: Sequence[str], labels: Optional[Dict[str, str]] = None) -> List[Tuple[str, str]]:
    """Keep already-selected values visible in pickers even if no longer offered."""
    return [(v, (labels or {}).get(v, v)) for v in values]
