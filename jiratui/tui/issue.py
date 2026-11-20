"""Issue detail screen: fields, wiki-rendered description, sub-tasks, links,
comments; transition, comment and assign actions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, List, Optional

from rich.console import Group
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Static

from .. import wiki
from ..api.models import FieldCatalog, Issue, PullRequest, format_points, format_ts
from .common import BaseScreen, PickerScreen, TextPromptScreen, error_text, guarded, ui_timeout
from .issuetable import CATEGORY_STYLE, PRIORITY_STYLE

if TYPE_CHECKING:  # pragma: no cover
    from .app import JiraApp

UNASSIGN = "__unassign__"


PR_STYLE = {"OPEN": "green", "MERGED": "magenta", "DECLINED": "red"}


def render_issue(issue: Issue, catalog: Optional[FieldCatalog], url: str = "",
                 pull_requests: Optional[List[PullRequest]] = None, pr_error: str = "") -> Group:
    head = Text()
    head.append(f"{issue.key}  ", style="bold magenta")
    head.append(issue.summary + "\n", style="bold")

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column()
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column()

    def who(u) -> Text:
        return Text(u.label()) if u else Text("Unassigned", style="dim italic")

    prio = issue.priority.name if issue.priority else ""
    grid.add_row("Type", Text(issue.issue_type.name), "Status",
                 Text(issue.status.name, style=CATEGORY_STYLE.get(issue.status.category_key, "")))
    grid.add_row("Priority", Text(prio or "-", style=PRIORITY_STYLE.get(prio, "")), "Resolution",
                 Text(issue.resolution or "Unresolved", style="" if issue.resolution else "dim"))
    grid.add_row("Assignee", who(issue.assignee), "Reporter", who(issue.reporter))
    grid.add_row("Project", Text(f"{issue.project_key} {issue.project_name}".strip()), "Labels",
                 Text(", ".join(issue.labels) or "-", style="cyan" if issue.labels else "dim"))
    grid.add_row("Sprint", Text(issue.sprint_label or "-"), "Story points",
                 Text(format_points(issue.story_points) or "-"))
    grid.add_row("Epic link", Text(issue.epic_link or "-"), "Parent",
                 Text(f"{issue.parent.key} {issue.parent.summary}" if issue.parent else "-"))
    grid.add_row("Components", Text(", ".join(issue.components) or "-"), "Fix versions",
                 Text(", ".join(issue.fix_versions) or "-"))
    grid.add_row("Created", Text(format_ts(issue.created)), "Updated", Text(format_ts(issue.updated)))
    parts: List = [head, grid]
    if url:
        parts.append(Text(f"\n{url}", style="underline dim"))

    parts.append(Text("\nDescription", style="bold underline"))
    parts.append(wiki.render(issue.description))

    if issue.subtasks:
        parts.append(Text(f"\nSub-tasks ({len(issue.subtasks)})", style="bold underline"))
        for s in issue.subtasks:
            t = Text("  ")
            t.append(s.key, style="bold")
            t.append(f"  {s.summary}  ")
            t.append(f"[{s.status}]", style="dim")
            parts.append(t)
    if issue.links:
        parts.append(Text(f"\nLinks ({len(issue.links)})", style="bold underline"))
        for ln in issue.links:
            t = Text("  ")
            t.append(f"{ln.relation} ", style="italic")
            t.append(ln.issue.key, style="bold")
            t.append(f"  {ln.issue.summary}  ")
            t.append(f"[{ln.issue.status}]", style="dim")
            parts.append(t)

    if pr_error:
        parts.append(Text("\nPull requests", style="bold underline"))
        parts.append(Text(f"Unavailable: {pr_error}", style="dim italic"))
    elif pull_requests:
        parts.append(Text(f"\nPull requests ({len(pull_requests)})", style="bold underline"))
        for pr in pull_requests:
            t = Text("  ")
            t.append(f"{pr.id} ", style="bold")
            t.append(f"{pr.title}  ")
            t.append(f"[{pr.status}]", style=PR_STYLE.get(pr.status, "dim"))
            t.append(f"  {pr.source} -> {pr.destination}  {pr.author}\n    ", style="dim")
            t.append(pr.url, style="underline dim")
            parts.append(t)

    shown = len(issue.comments)
    more = f", showing {shown}" if issue.comment_total > shown else ""
    parts.append(Text(f"\nComments ({issue.comment_total}{more})", style="bold underline"))
    if not issue.comments:
        parts.append(Text("No comments.", style="dim"))
    for c in issue.comments:
        h = Text()
        h.append(c.author.display_name or c.author.name, style="bold")
        h.append(f"  {format_ts(c.created)}", style="dim")
        parts.append(h)
        parts.append(Padding(wiki.render(c.body), (0, 0, 0, 2)))
        parts.append(Text(""))
    return Group(*parts)


class IssueDetailScreen(BaseScreen):
    BINDINGS = [
        Binding("t", "transition", "Transition"),
        Binding("n", "comment", "Comment"),
        Binding("a", "assign", "Assign"),
        Binding("g", "goto", "Go to linked"),
        Binding("R", "refresh", "Refresh"),
    ]

    DEFAULT_CSS = """
    IssueDetailScreen VerticalScroll { padding: 0 1; }
    IssueDetailScreen #issue-status { height: auto; padding: 0 1; }
    IssueDetailScreen #issue-status.error { color: $error; }
    """

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key
        self.issue: Optional[Issue] = None
        self.catalog: Optional[FieldCatalog] = None
        self.pull_requests: List[PullRequest] = []
        self.pr_error = ""

    @property
    def japp(self) -> "JiraApp":
        return self.app  # type: ignore[return-value]

    @property
    def client(self):
        return self.japp.require_client()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="issue-status")
        with VerticalScroll(id="issue-scroll"):
            yield Static("", id="issue-body")
        yield Footer()

    def on_mount(self) -> None:
        self.title = self.key
        self.sub_title = self.japp.session_label()
        self.query_one("#issue-scroll").focus()
        self.action_refresh()

    def _status(self, text: str, error: bool = False) -> None:
        st = self.query_one("#issue-status", Static)
        st.update(Text(text))
        st.set_class(error, "error")

    def action_refresh(self) -> None:
        self.run_api(self._load(), group="issue")

    async def _load(self) -> None:
        self._status(f"Loading {self.key}…  (Esc to cancel)")
        try:
            if self.catalog is None:
                self.catalog = await guarded(self.japp.catalog(), ui_timeout(self))
            self.issue = await guarded(self.client.get_issue(self.key, self.catalog), ui_timeout(self))
            try:
                self.pull_requests = await guarded(self.client.pull_requests(self.issue.id), ui_timeout(self))
                self.pr_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the integration is optional; keep the issue usable
                self.pull_requests, self.pr_error = [], error_text(exc).splitlines()[0]
        except asyncio.CancelledError:
            self._status("Cancelled. Press R to refresh.", True)
            raise
        except Exception as exc:  # noqa: BLE001
            self._status(error_text(exc) + "\nPress R to retry.", True)
            return
        self.title = f"{self.issue.key}: {self.issue.summary}"
        self.query_one("#issue-body", Static).update(
            render_issue(self.issue, self.catalog, self.client.browse_url(self.issue.key),
                         self.pull_requests, self.pr_error))
        self._status("t transition · n comment · a assign · g go to sub-task/link · R refresh · Esc back")

    # ------------------------------------------------------------ actions
    def action_transition(self) -> None:
        client = self.client
        key = self.key

        async def load():
            ts = await client.transitions(key)
            return [(t.id, f"{t.name}  →  {t.to_status}" if t.to_status else t.name) for t in ts]

        def chosen(v: Optional[List[str]]) -> None:
            if v:
                self.run_api(self._mutate(client.transition(key, v[0]), "Transition done."), group="issue-action")

        self.app.push_screen(PickerScreen(f"Transition {key}", load), chosen)

    def action_comment(self) -> None:
        def submitted(text: Optional[str]) -> None:
            if text:
                self.run_api(self._mutate(self.client.add_comment(self.key, text), "Comment added."),
                             group="issue-action")

        self.app.push_screen(TextPromptScreen(f"Comment on {self.key} (Jira wiki markup)"), submitted)

    def action_assign(self) -> None:
        client = self.client
        key = self.key
        me = self.japp.username

        async def load():
            users = await client.assignable_users(issue_key=key)
            return [(u.name, u.label()) for u in users]

        pinned = [(UNASSIGN, "Unassigned")] + ([(me, f"Assign to me ({me})")] if me else [])

        def chosen(v: Optional[List[str]]) -> None:
            if v:
                who = None if v[0] == UNASSIGN else v[0]
                self.run_api(self._mutate(client.assign(key, who), f"Assigned to {who or 'nobody'}."),
                             group="issue-action")

        self.app.push_screen(PickerScreen(f"Assign {key}", load, pinned=pinned), chosen)

    async def _mutate(self, coro, ok: str) -> None:
        self._status("Saving…  (Esc to cancel; the server may still apply it)")
        try:
            await guarded(coro, ui_timeout(self))
        except asyncio.CancelledError:
            self._status("Cancelled; press R to see whether the change was applied.", True)
            raise
        except Exception as exc:  # noqa: BLE001
            self._status(error_text(exc), True)
            return
        self.notify(ok)
        self.japp.issues_changed = True
        await self._load()

    def action_goto(self) -> None:
        if self.issue is None:
            return
        opts = [(s.key, f"sub-task  {s.key}  {s.summary}") for s in self.issue.subtasks]
        opts += [(ln.issue.key, f"{ln.relation}  {ln.issue.key}  {ln.issue.summary}") for ln in self.issue.links]
        if self.issue.parent:
            opts.insert(0, (self.issue.parent.key, f"parent  {self.issue.parent.key}  {self.issue.parent.summary}"))
        if self.issue.epic_link:
            opts.insert(0, (self.issue.epic_link, f"epic  {self.issue.epic_link}"))
        if not opts:
            self.notify("No linked issues or sub-tasks.", timeout=2)
            return

        def chosen(v: Optional[List[str]]) -> None:
            if v:
                self.app.push_screen(IssueDetailScreen(v[0]))

        self.app.push_screen(PickerScreen(f"Open from {self.key}", opts), chosen)
