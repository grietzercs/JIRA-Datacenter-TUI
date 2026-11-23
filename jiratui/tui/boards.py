"""Sprints and Backlog views.

Both are :class:`IssueListPane` subclasses: project and board pickers, the
shared label / people / custom filters (:class:`~jiratui.filters.FilterState`)
and an issue table. Only the base query differs:

* Sprints: ``sprint = <id> AND <filters> ORDER BY Rank ASC`` via ``/search``
  (falls back to the Agile ``/sprint/{id}/issue`` endpoint if the instance
  rejects the ``sprint`` JQL field);
* Backlog: the Agile ``/board/{id}/backlog`` endpoint with the filters passed
  as its ``jql`` parameter for scrum boards, or the JQL equivalent
  (``project = X AND statusCategory != Done``) for kanban boards and projects without a board.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Dict, List, Optional

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import Static

from .. import jql
from ..api.errors import JqlError
from ..api.models import Board, FieldCatalog, Issue, Page, Project, Sprint, User, format_date
from ..filters import CustomFilter, FilterState
from .common import PagedTable, PickerScreen, cell, error_text, guarded, ui_timeout
from .filterui import CustomFiltersScreen, FilterBar, FilterEditorScreen, pinned_for
from .issuetable import ISSUE_COLUMNS, IssueTable, issue_row

if TYPE_CHECKING:  # pragma: no cover
    from .app import JiraApp

NO_BOARD = "__no_board__"


class IssueListPane(Vertical):
    """Project/board context + shared filters + issue table."""

    BINDINGS = [
        Binding("p", "pick_project", "Project"),
        Binding("b", "pick_board", "Board"),
        Binding("l", "pick_labels", "Labels"),
        Binding("a", "pick_people", "People"),
        Binding("f", "custom_filters", "Custom filter"),
        Binding("F", "new_filter", "New filter", show=False),
        Binding("X", "clear_filters", "Clear filters", show=False),
        Binding("slash", "quick_search", "Search"),
    ]

    DEFAULT_CSS = """
    IssueListPane { height: 1fr; }
    IssueListPane .context { height: auto; padding: 0 1; color: $accent; }
    """

    view_name = "issues"
    order_by = "Rank ASC"

    def __init__(self, id: Optional[str] = None) -> None:
        super().__init__(id=id)
        self.project: Optional[Project] = None
        self.boards: List[Board] = []
        self.board: Optional[Board] = None
        self.filters = FilterState()
        self.people_labels: Dict[str, str] = {}
        self.sp_field = ""
        self.catalog: Optional[FieldCatalog] = None
        self.note = ""

    @property
    def japp(self) -> "JiraApp":
        return self.app  # type: ignore[return-value]

    @property
    def client(self):
        return self.japp.require_client()

    # ------------------------------------------------------------ widgets
    def context_bar(self) -> Static:
        return Static("", classes="context")

    def issue_table(self, empty_text: str, group=None) -> IssueTable:
        return IssueTable(ISSUE_COLUMNS, self._fetch_issues, issue_row, limit=50, autoload=False,
                          empty_text=empty_text, noun="issue", group=group,
                          filter_placeholder="Quick search: summary contains…  (/ to focus)", id=f"{self.view_name}-issues")

    @property
    def table(self) -> IssueTable:
        return self.query_one(IssueTable)

    def on_mount(self) -> None:
        self._update_context()
        self.table.show_message("Press p to choose a project.")

    def activate(self) -> None:
        """Called when the tab is shown: restore the last project on first use."""
        if self.project is None and not getattr(self, "_activated", False):
            self._activated = True
            last = self.japp.settings.last_project
            if last:
                self.run_api(self._restore_project(last))
        self.focus_main()

    def focus_main(self) -> None:
        self.table.focus_table()

    def run_api(self, coro) -> None:
        self.screen.run_api(coro, group=f"{self.view_name}-ctx")  # type: ignore[attr-defined]

    # ------------------------------------------------------------ context
    def _update_context(self) -> None:
        t = Text()
        t.append("Project: ", style="bold")
        t.append(self.project.label() if self.project else "none", style="" if self.project else "dim")
        t.append(" [p]   ", style="dim")
        t.append("Board: ", style="bold")
        if self.board:
            t.append(self.board.label())
        else:
            t.append("none" if self.project is None else "(no board: JQL)", style="dim")
        t.append(" [b]", style="dim")
        if self.sp_field and self.catalog is not None:
            t.append(f"   Story points: {self.catalog.name_of(self.sp_field)} ({self.sp_field})", style="dim")
        self.query_one(".context", Static).update(t)

    def effective_jql(self) -> str:  # pragma: no cover - overridden
        return ""

    def _update_filter_bar(self) -> None:
        self.query_one(FilterBar).show(self.filters, self.effective_jql(), self.people_labels, self.note)

    def refresh_issues(self) -> None:
        self._update_filter_bar()
        if self.ready_for_issues():
            self.table.reload()

    def ready_for_issues(self) -> bool:
        return self.project is not None

    async def _restore_project(self, key: str) -> None:
        try:
            projects = await guarded(self.japp.cached("projects", self.client.list_projects), ui_timeout(self))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.table.show_message(f"Could not load projects: {error_text(exc)}\nPress p to retry.", True)
            return
        match = next((p for p in projects if p.key.upper() == key.upper()), None)
        if match is not None and self.project is None:
            await self._load_project(match)

    def action_pick_project(self) -> None:
        async def load():
            projects = await self.japp.cached("projects", self.client.list_projects)
            self._projects = {p.key: p for p in projects}
            return [(p.key, p.label()) for p in projects]

        def chosen(values: Optional[List[str]]) -> None:
            if values:
                project = getattr(self, "_projects", {}).get(values[0])
                if project is not None:
                    self.run_api(self._load_project(project))

        self.app.push_screen(PickerScreen("Choose a project", load), chosen)

    async def _load_project(self, project: Project) -> None:
        self.project = project
        self.board = None
        self.boards = []
        self.filters = FilterState()
        self.people_labels = {}
        self.japp.remember_project(project.key)
        self._update_context()
        self.table.show_message(f"Loading boards for {project.key}…  (Esc to cancel)")
        try:
            self.catalog = await guarded(self.japp.catalog(), ui_timeout(self))
            self.boards = await guarded(
                self.japp.cached(f"boards:{project.key}", lambda: self.client.all_boards(project.key)),
                ui_timeout(self))
        except asyncio.CancelledError:
            self.table.show_message("Cancelled. Press p to choose the project again.", True)
            raise
        except Exception as exc:  # noqa: BLE001
            self.table.show_message(f"Could not load boards: {error_text(exc)}\nPress p to retry.", True)
            return
        await self._set_board(self.default_board(self.boards))

    def default_board(self, boards: List[Board]) -> Optional[Board]:
        scrum = [b for b in boards if b.has_sprints]
        return (scrum or boards or [None])[0]

    def action_pick_board(self) -> None:
        if self.project is None:
            self.notify("Choose a project first (p).", severity="warning")
            return
        opts = [(str(b.id), b.label()) for b in self.boards] + self.extra_board_choices()
        if not opts:
            self.notify(f"{self.project.key} has no boards.", severity="warning")
            return

        def chosen(values: Optional[List[str]]) -> None:
            if values:
                board = next((b for b in self.boards if str(b.id) == values[0]), None)
                self.run_api(self._set_board(board))

        self.app.push_screen(PickerScreen(f"Boards of {self.project.key}", opts), chosen)

    def extra_board_choices(self) -> List[tuple]:
        return []

    async def _set_board(self, board: Optional[Board]) -> None:
        self.board = board
        self.sp_field = self.catalog.story_points_id if self.catalog else ""
        if board is not None:
            try:
                est = await guarded(self.japp.cached(f"estimation:{board.id}",
                                                     lambda: self.client.board_estimation_field(board.id)),
                                    ui_timeout(self))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - optional: fall back to the field catalog
                est = ""
            self.sp_field = est or self.sp_field
        self._update_context()
        self.board_changed()

    def board_changed(self) -> None:  # pragma: no cover - overridden
        self.refresh_issues()

    # ------------------------------------------------------------ filters
    def _need_project(self) -> bool:
        if self.project is None:
            self.notify("Choose a project first (p).", severity="warning")
            return False
        return True

    def action_pick_labels(self) -> None:
        if not self._need_project():
            return
        key = self.project.key
        client = self.client

        async def load():
            labels = await self.japp.cached(f"labels:{key}", lambda: client.project_labels(key))
            labels = sorted(set(labels + await self.extra_labels()), key=lambda lb: (lb.lower(), lb))
            if not labels:  # nothing labelled yet in this project: offer Jira's global suggestions
                labels = await self.japp.cached("labels:*", lambda: client.label_suggestions(""))
            return [(lb, lb) for lb in labels]

        def chosen(values: Optional[List[str]]) -> None:
            if values is not None:
                self.filters.labels = values
                self.refresh_issues()

        self.app.push_screen(PickerScreen(
            f"Labels used in {key} (any of)", load, multi=True, selected=self.filters.labels,
            pinned=pinned_for(self.filters.labels),
            note="Labels are gathered from this project's issues (bounded to 1000)."), chosen)

    async def extra_people(self) -> List[User]:
        """People to offer beyond the project's assignable users."""
        return [await self.japp.cached("me", self.client.myself)]

    async def extra_labels(self) -> List[str]:
        """Labels to offer beyond those used in the project."""
        return []

    def action_pick_people(self) -> None:
        if not self._need_project():
            return
        key = self.project.key
        client = self.client

        async def load():
            users = await self.japp.cached(f"people:{key}", lambda: client.assignable_users(project_key=key))
            users = users + await self.extra_people()
            for u in users:
                self.people_labels[u.name] = u.display_name
            return [(u.name, u.label()) for u in {u.name: u for u in users}.values()]

        def chosen(values: Optional[List[str]]) -> None:
            if values is not None:
                self.filters.people = values
                self.refresh_issues()

        pinned = [(jql.UNASSIGNED, "Unassigned (assignee is EMPTY)")] + \
            [p for p in pinned_for(self.filters.people, self.people_labels) if p[0] != jql.UNASSIGNED]
        self.app.push_screen(PickerScreen(f"Assignee in {key} (any of)", load, multi=True,
                                          selected=self.filters.people, pinned=pinned), chosen)

    def action_custom_filters(self) -> None:
        if not self._need_project():
            return
        active = self.filters.custom.id if self.filters.custom else ""

        def done(result) -> None:
            if result is None:
                return
            action, flt = result
            self.filters.custom = flt if action == "apply" else None
            self.refresh_issues()

        self.app.push_screen(CustomFiltersScreen(self.project.key, active), done)

    def action_new_filter(self) -> None:
        if not self._need_project():
            return

        def saved(f: Optional[CustomFilter]) -> None:
            if f is not None:
                self.japp.filter_store.upsert(f)
                self.notify(f"Saved filter '{f.name}' and applied it.")
                self.filters.custom = f
                self.refresh_issues()

        self.app.push_screen(FilterEditorScreen(self.project.key), saved)

    def action_quick_search(self) -> None:
        self.table.query_one(".filter").focus()

    def fragments(self, quick: str) -> List[str]:
        """Filter fragments plus the quick search (summary contains every typed word)."""
        words = quick.split()
        return [*self.filters.fragments(), *([f"summary ~ {jql.quote_value(' '.join(words) + '*')}"] if words else [])]

    def action_clear_filters(self) -> None:
        self.filters.clear()
        self.refresh_issues()

    # ------------------------------------------------------------ issues
    async def _fetch_issues(self, start: int, limit: int, flt: str) -> Page[Issue]:  # pragma: no cover
        raise NotImplementedError

    def _fields(self) -> List[str]:
        cat = self.catalog or FieldCatalog()
        return cat.issue_fields([self.sp_field] if self.sp_field else [])


# ------------------------------------------------------------------ sprints
class SprintsPane(IssueListPane):
    BINDINGS = [
        Binding("x", "toggle_closed", "Closed sprints"),
        Binding("s", "focus_sprints", "Sprint list", show=False),
    ]

    DEFAULT_CSS = """
    SprintsPane > Horizontal { height: 1fr; }
    SprintsPane #sprint-list { width: 44; min-width: 30; border-right: solid $primary; }
    SprintsPane #sprint-right { width: 1fr; }
    """

    view_name = "sprints"

    def __init__(self, id: Optional[str] = None) -> None:
        super().__init__(id=id)
        self.sprint: Optional[Sprint] = None
        self.show_closed = False
        self._pick_timer: Optional[Timer] = None
        self.used_agile_fallback = False

    def compose(self) -> ComposeResult:
        yield self.context_bar()
        with Horizontal():
            yield PagedTable(["Sprint", "State", "Dates"], self._fetch_sprints, self._sprint_row, limit=500,
                             autoload=False, empty_text="No sprints.", noun="sprint", id="sprint-list")
            with Vertical(id="sprint-right"):
                yield FilterBar()
                yield self.issue_table("No issues in this sprint match the filters.")

    @property
    def sprint_table(self) -> PagedTable:
        return self.query_one("#sprint-list", PagedTable)

    def on_mount(self) -> None:
        super().on_mount()
        self.sprint_table.show_message("Press p to choose a project.")
        self._update_filter_bar()

    def focus_main(self) -> None:
        if self.sprint is None:
            self.sprint_table.focus_table()
        else:
            self.table.focus_table()

    def action_focus_sprints(self) -> None:
        self.sprint_table.focus_table()

    def default_board(self, boards: List[Board]) -> Optional[Board]:
        return next((b for b in boards if b.has_sprints), None) or super().default_board(boards)

    def board_changed(self) -> None:
        self.sprint = None
        self._update_filter_bar()
        if self.board is None or not self.board.has_sprints:
            what = "has no scrum board" if self.board is None else f"board '{self.board.name}' is {self.board.type}"
            msg = f"{self.project.key if self.project else '?'} {what}: no sprints. Use the Backlog or Issues view."
            self.sprint_table.show_message(msg)
            self.table.show_message(msg)
            return
        self.table.show_message("Choose a sprint on the left.")
        self.sprint_table.reload()

    async def _fetch_sprints(self, start: int, limit: int, flt: str) -> Page[Sprint]:
        board = self.board
        if board is None:
            return Page(values=[])
        sprints = await self.client.board_sprints(board.id, include_closed=self.show_closed)
        return Page(values=sprints, start_at=0, max_results=len(sprints), total=len(sprints), is_last=True)

    @staticmethod
    def _sprint_row(s: Sprint):
        style = {"active": "bold green", "future": "cyan", "closed": "dim"}.get(s.state, "")
        end = format_date(s.end_date)
        dates = f"{format_date(s.start_date)}→{end[5:] if end[:4] == s.start_date[:4] else end}" if s.start_date else ""
        return (cell(s.name, "bold" if s.state == "active" else ""), cell(s.state, style), cell(dates, "dim"))

    def action_toggle_closed(self) -> None:
        if self.board is None or not self.board.has_sprints:
            return
        self.show_closed = not self.show_closed
        self.notify("Showing closed sprints (most recent first)." if self.show_closed else "Hiding closed sprints.",
                    timeout=2)
        self.sprint_table.reload()

    @on(PagedTable.Highlighted, "#sprint-list")
    def _sprint_highlighted(self, ev: PagedTable.Highlighted) -> None:
        ev.stop()
        # Debounce: browsing the list with the cursor should not fire a search per row.
        if self._pick_timer is not None:
            self._pick_timer.stop()
        item = ev.item
        self._pick_timer = self.set_timer(0.3, lambda: self.select_sprint(item))

    @on(PagedTable.Selected, "#sprint-list")
    def _sprint_selected(self, ev: PagedTable.Selected) -> None:
        ev.stop()
        if self._pick_timer is not None:
            self._pick_timer.stop()
        self.select_sprint(ev.item, force=True)
        self.table.focus_table()

    def select_sprint(self, sprint: Sprint, force: bool = False) -> None:
        if not force and self.sprint is not None and self.sprint.id == sprint.id:
            return
        self.sprint = sprint
        self.used_agile_fallback = False
        self.table.empty_text = f"No issues in {sprint.name} match the filters."
        self.refresh_issues()

    def ready_for_issues(self) -> bool:
        return self.sprint is not None

    def effective_jql(self, quick: str = "") -> str:
        if self.sprint is None:
            return ""
        return jql.compose(jql.sprint_clause(self.sprint.id), self.fragments(quick), self.order_by)

    async def _fetch_issues(self, start: int, limit: int, flt: str) -> Page[Issue]:
        sprint = self.sprint
        if sprint is None:
            return Page(values=[])
        if not self.used_agile_fallback:
            try:
                return await self.client.search(self.effective_jql(flt), start, limit, fields=self._fields(),
                                                catalog=self.catalog, story_points_id=self.sp_field)
            except JqlError as exc:
                if not any("sprint" in m.lower() for m in exc.error_messages) or not self._sprint_clause_failed(exc):
                    raise
                self.used_agile_fallback = True
                self.note = ("This instance rejected `sprint = N` in JQL; using /rest/agile/1.0/sprint/{id}/issue "
                             "with the filters as its jql parameter.")
                self._update_filter_bar()
        return await self.client.sprint_issues(sprint.id, jql.compose("", self.fragments(flt), self.order_by),
                                               start, limit, fields=self._fields(), catalog=self.catalog,
                                               story_points_id=self.sp_field)

    async def extra_people(self) -> List[User]:
        sprint = self.sprint
        if sprint is None:
            return []
        return await self.japp.cached(f"people:sprint:{sprint.id}", lambda: self.client.sprint_assignees(sprint.id)) \
            + await super().extra_people()

    @staticmethod
    def _sprint_clause_failed(exc: JqlError) -> bool:
        text = " ".join(exc.error_messages).lower()
        return "field 'sprint'" in text or "'sprint' does not exist" in text


# ------------------------------------------------------------------ backlog
class BacklogPane(IssueListPane):
    view_name = "backlog"

    def __init__(self, id: Optional[str] = None) -> None:
        super().__init__(id=id)
        self._grouped: List[Issue] = []
        self._group_counts: Dict[int, int] = {}

    def compose(self) -> ComposeResult:
        yield self.context_bar()
        yield FilterBar()
        yield self.issue_table("The backlog is empty (or nothing matches the filters).", group=self._group_label)

    def on_mount(self) -> None:
        super().on_mount()
        self._update_filter_bar()

    def extra_board_choices(self) -> List[tuple]:
        return [(NO_BOARD, "(no board: JQL equivalent of a backlog)")]

    def uses_agile(self) -> bool:
        return self.board is not None and self.board.has_sprints

    def effective_jql(self, quick: str = "") -> str:
        if self.project is None:
            return ""
        if self.uses_agile():
            frag = jql.and_join(self.fragments(quick))
            return f"[board #{self.board.id} backlog via Agile API] {frag}".strip()
        return jql.compose(jql.backlog_base(self.project.key), self.fragments(quick), self.order_by)

    async def extra_people(self) -> List[User]:
        mine = await super().extra_people()
        if not self.uses_agile():
            return mine
        board_id = self.board.id
        users, _ = await self.japp.cached(f"facets:{board_id}", lambda: self.client.backlog_facets(board_id))
        return users + mine

    async def extra_labels(self) -> List[str]:
        if not self.uses_agile():
            return []
        board_id = self.board.id
        _, labels = await self.japp.cached(f"facets:{board_id}", lambda: self.client.backlog_facets(board_id))
        return labels

    def board_changed(self) -> None:
        self.note = "" if self.uses_agile() or self.project is None else (
            "No scrum board selected: using the JQL equivalent of a backlog (kanban boards have no Agile backlog "
            "endpoint unless the backlog is enabled).")
        self.refresh_issues()

    @staticmethod
    def _sprint_of(issue: Issue) -> Optional[Sprint]:
        """The active or future sprint an issue is planned in (closed history does not count)."""
        return next((s for s in sorted(issue.sprints, key=lambda s: s.state != "active")
                     if s.state in ("active", "future")), None)

    def _group_label(self, issue: Issue) -> str:
        sprint = self._sprint_of(issue)
        count = self._group_counts.get(sprint.id if sprint else 0, 0)
        noun = f"{count} issue{'s' if count != 1 else ''}"
        if sprint is None:
            return f"Backlog  ({noun})"
        dates = f"  {format_date(sprint.start_date)} - {format_date(sprint.end_date)}" if sprint.start_date else ""
        return f"{sprint.name} [{sprint.state}]{dates}  ({noun})"

    async def _fetch_issues(self, start: int, limit: int, flt: str) -> Page[Issue]:
        """Whole (bounded) backlog, ordered active sprint, future sprints, then unscheduled,
        so the table can show a header per sprint; rank order is kept within each group."""
        if self.project is None:
            return Page(values=[])
        if start == 0:
            items: List[Issue] = []
            while len(items) < 1000:
                page = await self._fetch_page(len(items), 100, flt)
                items += page.values
                if page.next_start is None or not page.values:
                    break
            sprints = {i.key: self._sprint_of(i) for i in items}

            def order(issue: Issue):
                s = sprints[issue.key]
                return (True, False, "", 0) if s is None else (False, s.state != "active", s.start_date, s.id)

            self._grouped = sorted(items, key=order)
            self._group_counts = {}
            for s in sprints.values():
                self._group_counts[s.id if s else 0] = self._group_counts.get(s.id if s else 0, 0) + 1
        items = self._grouped
        return Page(values=items[start:start + limit], start_at=start, max_results=limit, total=len(items),
                    is_last=start + limit >= len(items))

    async def _fetch_page(self, start: int, limit: int, flt: str) -> Page[Issue]:
        if self.uses_agile():
            # The Agile backlog endpoint omits issues planned in sprints and its jql parameter returns
            # nothing for filtered views on some servers; search the board's saved filter instead.
            fid = await self.japp.cached(f"boardfilter:{self.board.id}",
                                         lambda: self.client.board_filter_id(self.board.id))
            if fid:
                base = f"filter = {int(fid)} AND {jql.project_clause(self.project.key)} AND statusCategory != Done"
                return await self.client.search(
                    jql.compose(base, self.fragments(flt), self.order_by), start, limit,
                    fields=self._fields(), catalog=self.catalog, story_points_id=self.sp_field)
        if self.uses_agile():
            return await self.client.board_backlog(
                self.board.id, jql.and_join(self.fragments(flt)), start, limit,
                fields=[*self._fields(), "sprint"], catalog=self.catalog, story_points_id=self.sp_field)
        return await self.client.search(self.effective_jql(flt), start, limit, fields=self._fields(),
                                        catalog=self.catalog, story_points_id=self.sp_field)
