"""Main screen: Sprints / Backlog / Issues (JQL) tabs. Enter on any issue opens it."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING, Optional, Union

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from .boards import BacklogPane, IssueListPane, SprintsPane
from .common import BaseScreen, ConfirmScreen, PagedTable
from .issuetable import IssueTable
from .search import IssuesPane

if TYPE_CHECKING:  # pragma: no cover
    from .app import JiraApp

Pane = Union[IssueListPane, IssuesPane]


class MainScreen(BaseScreen):
    BINDINGS = [
        Binding("f2", "tab('sprints')", "Sprints"),
        Binding("f3", "tab('backlog')", "Backlog"),
        Binding("f4", "tab('issues')", "Issues/JQL"),
        Binding("1", "tab('sprints')", "Sprints", show=False),
        Binding("2", "tab('backlog')", "Backlog", show=False),
        Binding("3", "tab('issues')", "Issues", show=False),
        Binding("y", "copy_link", "Copy link"),
        Binding("ctrl+n", "create", "Create issue"),
    ]

    DEFAULT_CSS = """
    MainScreen #info { height: auto; padding: 0 1; color: $text-muted; }
    MainScreen TabbedContent { height: 1fr; }
    MainScreen TabbedContent > ContentSwitcher { height: 1fr; }
    MainScreen TabPane { padding: 0; height: 1fr; }
    """

    @property
    def japp(self) -> "JiraApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="info")
        with TabbedContent(initial="sprints"):
            with TabPane("Sprints (F2)", id="sprints"):
                yield SprintsPane(id="sprints-pane")
            with TabPane("Backlog (F3)", id="backlog"):
                yield BacklogPane(id="backlog-pane")
            with TabPane("Issues / JQL (F4)", id="issues"):
                yield IssuesPane(id="issues-pane")
        yield Footer()

    def on_mount(self) -> None:
        info = self.japp.server
        self.title = "Jira"
        self.sub_title = self.japp.session_label()
        lines = [f"Connected to {info.base_url}  ·  {info.describe()}  ·  user: {info.username or '?'}"
                 "  ·  F1 help · Ctrl+N create · y copy link · Esc: log out"]
        for w in info.warnings:
            lines.append(f"⚠ {w}")
        self.query_one("#info", Static).update(Text("\n".join(lines)))
        self.call_after_refresh(self._activate_current)

    def pane(self, name: Optional[str] = None) -> Pane:
        name = name or self.query_one(TabbedContent).active
        return self.query_one(f"#{name}-pane")  # type: ignore[return-value]

    def _activate_current(self) -> None:
        self.pane().activate()

    @on(TabbedContent.TabActivated)
    def _tab_activated(self, ev: TabbedContent.TabActivated) -> None:
        if ev.pane is not None and ev.pane.id:
            self.pane(ev.pane.id).activate()

    def action_tab(self, name: str) -> None:
        self.query_one(TabbedContent).active = name

    def on_screen_resume(self) -> None:
        if self.japp.issues_changed:
            self.japp.issues_changed = False
            pane = self.pane()
            table = pane.query_one(IssueTable)
            if table.loaded_once:
                table.reload()

    @on(PagedTable.Selected, "IssueTable")
    def _open_issue(self, ev: PagedTable.Selected) -> None:
        from .issue import IssueDetailScreen

        self.app.push_screen(IssueDetailScreen(ev.item.key))

    def action_back(self) -> None:
        """Esc cancels a running request; otherwise ask before logging out
        (a stray Esc while typing JQL should not end the session)."""
        if self.cancel_loading():
            self.notify("Cancelled.", timeout=2)
            return

        def confirmed(ok: Optional[bool]) -> None:
            if ok and self.app.screen is self:
                self.app.pop_screen()

        self.app.push_screen(ConfirmScreen("Log out and return to the login screen?", "Log out"), confirmed)

    def action_copy_link(self) -> None:
        """Copy the web UI link of the highlighted issue to the system clipboard."""
        issue = self.pane().query_one(IssueTable).current_item()
        if issue is None:
            self.notify("No issue highlighted.", severity="warning")
            return
        url = self.japp.require_client().browse_url(issue.key)
        # OSC 52 reaches the clipboard of terminals that support it, including over SSH; the
        # native tool covers terminals that do not.
        self.app.copy_to_clipboard(url)
        tool = next(([t, *args] for t, args in (("pbcopy", []), ("wl-copy", []), ("xclip", ["-selection", "clipboard"]))
                     if shutil.which(t)), None)
        if tool:
            subprocess.run(tool, input=url.encode(), check=False, timeout=5)
        self.notify(f"Copied {url}", timeout=3)

    def action_create(self) -> None:
        from .create import CreateIssueScreen

        pane = self.pane()
        project = getattr(pane, "project", None)
        key = project.key if project is not None else self.japp.settings.last_project
        self.app.push_screen(CreateIssueScreen(key))
