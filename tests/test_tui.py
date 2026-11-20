"""Headless TUI tests (Textual Pilot) against the fake Jira server:
login → sprints → label/person/custom filters → backlog → JQL search →
issue detail → create issue, plus cancellation and timeouts."""

import asyncio
import json

from rich.console import Console
from textual.widgets import Input, RadioButton, Select, SelectionList, Static, TextArea

from jiratui import jql
from jiratui.config import EnvCredentials, Settings
from jiratui.testing.fake_server import DEMO_PASSWORD, DEMO_TOKEN, DEMO_URL, FakeJira
from jiratui.tui.app import JiraApp
from jiratui.tui.boards import BacklogPane, SprintsPane
from jiratui.tui.common import ConfirmScreen, PickerScreen, TextPromptScreen
from jiratui.tui.create import CreateIssueScreen
from jiratui.tui.filterui import CustomFiltersScreen, FilterBar, FilterEditorScreen
from jiratui.tui.issue import IssueDetailScreen
from jiratui.tui.login import LoginScreen
from jiratui.tui.main import MainScreen
from jiratui.tui.search import IssuesPane

SIZE = (150, 45)


def text_of(widget) -> str:
    content = widget.content if hasattr(widget, "content") else widget.renderable
    if isinstance(content, str):
        return content
    console = Console(width=220, color_system=None, force_terminal=False)
    with console.capture() as cap:
        console.print(content)
    return cap.get()


async def wait_for(pilot, cond, timeout=5.0, msg="condition"):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            if cond():
                return
        except Exception:
            pass
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {msg}")


def make_app(fake, secret="", last_project="PLAT", **kw):
    settings = Settings(base_url=DEMO_URL, use_macos_keychain_cas=False, last_project=last_project)
    creds = EnvCredentials(base_url=DEMO_URL, secret=secret, auth_method="bearer") if secret else EnvCredentials()
    return JiraApp(settings, env_creds=creds, env={}, transport_factory=fake.transport, ui_timeout=10, **kw)


async def login(app, pilot, token=DEMO_TOKEN):
    app.screen.query_one("#secret", Input).value = token
    await pilot.press("ctrl+s")
    await wait_for(pilot, lambda: isinstance(app.screen, MainScreen), msg="main screen")
    return app.screen


async def sprints_ready(app, pilot) -> SprintsPane:
    pane = app.screen.query_one(SprintsPane)
    await wait_for(pilot, lambda: pane.sprint is not None and pane.table.table.row_count > 0 and not
                   pane.table.is_loading, msg="sprint issues")
    return pane


async def pick_multi(app, pilot, filter_text, n_down=0):
    """In an open multi PickerScreen: filter, toggle the n-th visible option, apply."""
    await wait_for(pilot, lambda: isinstance(app.screen, PickerScreen), msg="picker")
    picker = app.screen
    await wait_for(pilot, lambda: picker.options, msg="picker options")
    picker.query_one("#pick-filter", Input).value = filter_text
    await pilot.pause()
    await pilot.press("enter")  # focus the list
    sl = picker.query_one(SelectionList)
    await wait_for(pilot, lambda: sl.has_focus, msg="list focus")
    sl.highlighted = n_down
    await pilot.press("space", "ctrl+s")
    await wait_for(pilot, lambda: not isinstance(app.screen, PickerScreen), msg="picker closed")


async def pick_single(app, pilot, filter_text):
    await wait_for(pilot, lambda: isinstance(app.screen, PickerScreen), msg="picker")
    picker = app.screen
    await wait_for(pilot, lambda: picker.options, msg="picker options")
    picker.query_one("#pick-filter", Input).value = filter_text
    await pilot.pause()
    await pilot.press("enter")
    await wait_for(pilot, lambda: app.screen is not picker, msg="picker closed")


# ---------------------------------------------------------------------- login
async def test_login_validation_and_bad_token(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        assert isinstance(app.screen, LoginScreen)
        status = app.screen.query_one("#status", Static)
        app.screen.query_one("#url", Input).value = "jira.example.test"
        app.screen.query_one("#secret", Input).value = "x" * 10
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: "https://" in text_of(status), msg="url validation error")
        app.screen.query_one("#url", Input).value = DEMO_URL + "/browse/PLAT-1"
        app.screen.query_one("#secret", Input).value = "wrong-token-123"
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: "401" in text_of(status), msg="401 error")
        assert "Connection failed" in text_of(status) and "invalid or expired" in text_of(status)
        assert app.screen.query_one("#url", Input).value == DEMO_URL  # pasted browse URL normalised
        assert isinstance(app.screen, LoginScreen)
        assert app.screen.query_one("#secret", Input).password


async def test_basic_requires_username_and_reports_seraph(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        scr = app.screen
        scr.query_one("#m-basic", RadioButton).value = True
        scr.query_one("#username", Input).value = ""
        scr.query_one("#secret", Input).value = DEMO_PASSWORD
        await pilot.press("ctrl+s")
        status = scr.query_one("#status", Static)
        await wait_for(pilot, lambda: "username is required" in text_of(status).lower())
        scr.query_one("#username", Input).value = "alice"
        scr.query_one("#secret", Input).value = "wrong-password"
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: "AUTHENTICATED_FAILED" in text_of(status), msg="seraph header surfaced")
        scr.query_one("#secret", Input).value = DEMO_PASSWORD
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: isinstance(app.screen, MainScreen))
        assert app.server.username == "alice"


async def test_detect_marks_pat_unsupported_on_old_server():
    fake = FakeJira(version="8.13.5")
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        scr = app.screen
        assert scr.query_one("#m-bearer", RadioButton).value  # default
        await pilot.press("ctrl+t")
        info = scr.query_one("#detect-info", Static)
        await wait_for(pilot, lambda: "NOT supported" in text_of(info), msg="detection")
        assert "8.13.5" in text_of(info)
        assert scr.query_one("#m-basic", RadioButton).value  # best method preselected
        assert "not supported" in str(scr.query_one("#m-bearer", RadioButton).label)
        # No credentials were sent while probing.
        assert all("authorization" not in r.headers for r in fake.requests)
        # The user may still override; the failure then explains why.
        scr.query_one("#m-bearer", RadioButton).value = True
        scr.query_one("#secret", Input).value = DEMO_TOKEN
        await pilot.press("ctrl+s")
        status = scr.query_one("#status", Static)
        await wait_for(pilot, lambda: "8.14" in text_of(status), msg="PAT version hint")


async def test_detect_preselects_bearer_on_new_server(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        scr = app.screen
        scr.query_one("#m-basic", RadioButton).value = True
        await pilot.click("#detect")
        info = scr.query_one("#detect-info", Static)
        await wait_for(pilot, lambda: "are supported" in text_of(info), msg="detection")
        assert scr.query_one("#m-bearer", RadioButton).value
        assert "supported (9.12.2)" in str(scr.query_one("#m-bearer", RadioButton).label)


# -------------------------------------------------------------------- sprints
async def test_sprints_view_and_label_person_filters(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot)
        pane = await sprints_ready(app, pilot)
        assert pane.project.key == "PLAT" and pane.board.id == 1 and pane.sprint.name == "PLAT Sprint 12"
        assert pane.table.table.row_count == 26 and pane.sp_field == "customfield_10002"
        assert [s.state for s in pane.sprint_table.items] == ["active", "future", "future"]
        assert fake.searches[-1] == "sprint = 12 ORDER BY Rank ASC"
        first = pane.table.items[0]
        assert first.key == "PLAT-3" and first.story_points == 3

        # --- label filter (options come from the project's issues)
        pane.table.focus_table()
        await pilot.press("l")
        await pick_multi(app, pilot, "ux")
        await wait_for(pilot, lambda: pane.filters.labels == ["ux/ui"] and not pane.table.is_loading)
        await wait_for(pilot, lambda: pane.table.items and all("ux/ui" in i.labels for i in pane.table.items))
        assert fake.searches[-1] == 'sprint = 12 AND labels in ("ux/ui") ORDER BY Rank ASC'
        bar = text_of(pane.query_one(FilterBar))
        assert 'labels in ("ux/ui")' in bar and "Labels: ux/ui" in bar

        # --- person filter: Unassigned is offered first
        await pilot.press("a")
        await pick_multi(app, pilot, "")
        await wait_for(pilot, lambda: pane.filters.people == [jql.UNASSIGNED] and not pane.table.is_loading)
        await wait_for(pilot, lambda: pane.table.items and all(i.assignee is None for i in pane.table.items))
        assert fake.searches[-1] == 'sprint = 12 AND labels in ("ux/ui") AND assignee is EMPTY ORDER BY Rank ASC'
        # add a person whose username needs quoting
        await pilot.press("a")
        await pick_multi(app, pilot, "mary")
        await wait_for(pilot, lambda: pane.filters.people == [jql.UNASSIGNED, "mary ann"])
        assert '(assignee in ("mary ann") OR assignee is EMPTY)' in text_of(pane.query_one(FilterBar))
        assert "Mary Ann Smith" in text_of(pane.query_one(FilterBar))

        # --- clear all
        await pilot.press("X")
        await wait_for(pilot, lambda: pane.filters.is_empty and pane.table.table.row_count == 26)

        # --- closed sprints toggle, choose another sprint
        await pilot.press("x")
        await wait_for(pilot, lambda: len(pane.sprint_table.items) == 9, msg="closed sprints listed")
        pane.sprint_table.focus_table()
        pane.sprint_table.table.move_cursor(row=1)
        await pilot.press("enter")
        await wait_for(pilot, lambda: pane.sprint.id == 13 and not pane.table.is_loading)
        assert {i.key for i in pane.table.items} >= {"PLAT-9", "PLAT-10"}


async def test_custom_filters_create_apply_edit_delete_and_favourites(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot)
        pane = await sprints_ready(app, pilot)
        pane.table.focus_table()

        # New filter (F): invalid JQL is rejected by the server and not saved.
        await pilot.press("F")
        await wait_for(pilot, lambda: isinstance(app.screen, FilterEditorScreen))
        ed = app.screen
        ed.query_one("#fe-name", Input).value = "High prio open"
        ed.query_one("#fe-jql", Input).value = "foo = bar"
        await pilot.press("ctrl+s")
        err = ed.query_one("#fe-error", Static)
        await wait_for(pilot, lambda: "Field 'foo' does not exist" in text_of(err), msg="jql rejected")
        assert app.filter_store.filters == []
        # Structured part via a picker + valid JQL.
        await pilot.click("#fe-status")
        await pick_multi(app, pilot, "progress")
        assert ed.flt.statuses == ["In Progress"]
        ed.query_one("#fe-jql", Input).value = "priority in (High, Highest)"
        await pilot.pause()
        assert 'status = "In Progress" AND priority in (High, Highest)' in text_of(ed.query_one("#fe-preview"))
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: not isinstance(app.screen, FilterEditorScreen), msg="saved")
        await wait_for(pilot, lambda: pane.filters.custom is not None and not pane.table.is_loading)
        assert [i.key for i in pane.table.items] == ["PLAT-3", "PLAT-14", "PLAT-19", "PLAT-29"] or \
            all(i.status.name == "In Progress" and i.priority.name in ("High", "Highest") for i in pane.table.items)
        assert fake.searches[-1] == ('sprint = 12 AND status = "In Progress" AND priority in (High, Highest) '
                                     "ORDER BY Rank ASC")
        saved = json.loads(open(app.filter_store.path).read())["filters"]
        assert saved[0]["name"] == "High prio open" and saved[0]["project_key"] == "PLAT"

        # Manager (f): favourites listed; apply one.
        await pilot.press("f")
        await wait_for(pilot, lambda: isinstance(app.screen, CustomFiltersScreen))
        mgr = app.screen
        await wait_for(pilot, lambda: len(mgr.entries) == 3, msg="favourites loaded")
        assert [e.name for e in mgr.entries] == ["High prio open", "My open issues", "High priority bugs"]
        mgr.query_one("#cf-list").highlighted = 2
        await pilot.press("enter")
        await wait_for(pilot, lambda: pane.filters.custom and pane.filters.custom.name == "High priority bugs")
        await wait_for(pilot, lambda: not pane.table.is_loading)
        assert all(i.issue_type.name == "Bug" for i in pane.table.items)
        assert "(issuetype = Bug AND priority in (Highest, High))" not in fake.searches[-1]  # no needless parens
        assert "issuetype = Bug AND priority in (Highest, High)" in fake.searches[-1]

        # Edit (e) the local filter: rename it.
        await pilot.press("f")
        await wait_for(pilot, lambda: isinstance(app.screen, CustomFiltersScreen))
        mgr = app.screen
        mgr.query_one("#cf-list").highlighted = 0
        await pilot.press("e")
        await wait_for(pilot, lambda: isinstance(app.screen, FilterEditorScreen))
        app.screen.query_one("#fe-name", Input).value = "Renamed"
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: app.screen is mgr and app.filter_store.filters[0].name == "Renamed")
        # Delete (d) with confirmation.
        mgr.query_one("#cf-list").highlighted = 0
        await pilot.press("d")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await wait_for(pilot, lambda: app.filter_store.filters == [], msg="deleted")
        # x = no custom filter
        await wait_for(pilot, lambda: app.screen is mgr)
        await pilot.press("x")
        await wait_for(pilot, lambda: pane.filters.custom is None)


# -------------------------------------------------------------------- backlog
async def test_backlog_view_agile_and_jql_equivalent(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        main = await login(app, pilot)
        await sprints_ready(app, pilot)
        await pilot.press("f3")
        pane = main.query_one(BacklogPane)
        await wait_for(pilot, lambda: pane.board is not None and pane.table.table.row_count > 0 and
                       not pane.table.is_loading, msg="backlog loaded")
        assert any(r.url.path == "/rest/agile/1.0/board/1/backlog" for r in fake.requests)
        assert pane.table.pager.total == 54
        assert all(not i.sprints or all(s.state == "closed" for s in i.sprints) for i in pane.table.items)

        # Same label filter component, sent as the Agile endpoint's jql parameter.
        pane.table.focus_table()
        await pilot.press("l")
        await pick_multi(app, pilot, "tech")
        await wait_for(pilot, lambda: pane.filters.labels == ["tech-debt"] and not pane.table.is_loading)
        req = [r for r in fake.requests if r.url.path.endswith("/backlog")][-1]
        assert req.url.params["jql"] == 'labels in ("tech-debt") ORDER BY Rank ASC'
        assert all("tech-debt" in i.labels for i in pane.table.items)

        # Kanban-only project: JQL equivalent of a backlog.
        await pilot.press("p")
        await pick_single(app, pilot, "OPS")
        await wait_for(pilot, lambda: pane.project.key == "OPS" and pane.board is not None and
                       not pane.table.is_loading and pane.table.loaded_once, msg="OPS backlog")
        assert pane.board.type == "kanban"
        assert pane.filters.is_empty  # filters are per project
        await wait_for(pilot, lambda: {i.key for i in pane.table.items} == {"OPS-1", "OPS-2"})
        assert fake.searches[-1].startswith('project = "OPS" AND (sprint is EMPTY OR sprint in closedSprints())')
        assert "JQL equivalent" in text_of(pane.query_one(FilterBar))


# ----------------------------------------------------------------------- JQL
async def test_jql_search_view(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        main = await login(app, pilot)
        await sprints_ready(app, pilot)
        await pilot.press("f4")
        pane = main.query_one(IssuesPane)
        await wait_for(pilot, lambda: pane.table.loaded_once and not pane.table.is_loading, msg="default query")
        assert pane.jql_input.value.startswith("assignee = currentUser()")
        assert pane.table.items and all(i.assignee.name == "alice" for i in pane.table.items)
        assert pane.jql_input.has_focus

        # Autocomplete: field names, then values from the suggestions endpoint.
        pane.jql_input.value = "project = PLAT AND sta"
        sugg = pane.query_one("#jql-suggest", Static)
        await wait_for(pilot, lambda: "status" in text_of(sugg), msg="field suggestions")
        await wait_for(pilot, lambda: pane.jql_input._suggestion == "project = PLAT AND status", msg="inline")
        pane.jql_input.value = 'project = PLAT AND labels in (rel'
        await wait_for(pilot, lambda: '"release:2.0"' in text_of(sugg), msg="value suggestions")

        # Invalid JQL: Jira's errorMessages are listed.
        pane.jql_input.value = "project = NOPE AND foo = 1"
        pane.jql_input.focus()
        await pilot.press("enter")
        err = pane.query_one("#jql-error", Static)
        await wait_for(pilot, lambda: "Field 'foo' does not exist" in text_of(err), msg="jql error")
        assert "The value 'NOPE' does not exist for the field 'project'." in text_of(err)
        assert "NOPE" not in app.query_store.history[0]  # failed queries are not added to history

        # Valid query across all types, paginated.
        pane.jql_input.value = "text ~ crash ORDER BY key ASC"
        pane.jql_input.focus()
        await pilot.press("enter")
        await wait_for(pilot, lambda: not pane.table.is_loading and pane.table.items and
                       pane.table.items[0].key == "MOB-5", msg="results")
        assert text_of(err).strip() == ""
        assert app.query_store.history[0] == "text ~ crash ORDER BY key ASC"
        pane.jql_input.value = "ORDER BY key ASC"
        pane.jql_input.focus()
        await pilot.press("enter")
        await wait_for(pilot, lambda: pane.table.table.row_count == 50 and not pane.table.pager.exhausted)
        assert pane.table.pager.total == len(fake.issues)
        pane.table.focus_table()
        await pilot.press("m")
        await wait_for(pilot, lambda: pane.table.table.row_count == 100, msg="page 2")

        # Issue-type chip (all types from /issuetype) is ANDed in.
        pane.jql_input.focus()
        await pilot.press("ctrl+y")
        await pick_multi(app, pilot, "Epic")
        await wait_for(pilot, lambda: pane.types == ["Epic"] and not pane.table.is_loading)
        assert {i.key for i in pane.table.items} == {"PLAT-1", "PLAT-2"}
        assert fake.searches[-1] == 'issuetype = "Epic" ORDER BY key ASC'

        # Order by picker rewrites ORDER BY.
        await pilot.press("ctrl+b")
        await pick_single(app, pilot, "created")
        await pick_single(app, pilot, "desc")
        await wait_for(pilot, lambda: pane.jql_input.value == "ORDER BY created DESC")

        # Insert helper: assignee = currentUser()
        await pilot.press("ctrl+t")
        await pick_single(app, pilot, "currentUser")
        assert pane.jql_input.value == "assignee = currentUser() ORDER BY created DESC"

        # Save + saved queries + history pickers.
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: isinstance(app.screen, TextPromptScreen))
        app.screen.query_one("#text", Input).value = "Mine"
        await pilot.press("enter")
        await wait_for(pilot, lambda: [s.name for s in app.query_store.saved] == ["Mine"])
        pane.jql_input.value = ""
        await pilot.press("ctrl+o")
        await pick_single(app, pilot, "Mine")
        await wait_for(pilot, lambda: pane.jql_input.value == "assignee = currentUser() ORDER BY created DESC")
        await pilot.press("ctrl+r")
        await pick_single(app, pilot, "text ~ crash")
        await wait_for(pilot, lambda: pane.query_text == "text ~ crash ORDER BY key ASC")


# -------------------------------------------------------------- issue detail
async def test_issue_detail_and_actions(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        main = await login(app, pilot)
        pane = await sprints_ready(app, pilot)
        pane.table.focus_table()
        await pilot.press("enter")  # PLAT-3 is first
        await wait_for(pilot, lambda: isinstance(app.screen, IssueDetailScreen))
        scr = app.screen
        body = scr.query_one("#issue-body", Static)
        await wait_for(pilot, lambda: "Login page throws 500" in text_of(body), msg="issue loaded")
        txt = text_of(body)
        for needle in ("In Progress", "Alice Anders (alice)", "Bob Brown (bob)", "backend, ux/ui", "PLAT Sprint 12",
                       "Story points", "PLAT-1", "1. Open the login page", "def login(user):", "Sub-tasks (1)",
                       "PLAT-4", "blocks PLAT-6", "Pull requests (2)", "#42 Fix session expiry handling", "[MERGED]", "bugfix/PLAT-3-session -> main",
                       "Comments (2)", "Reproduced on staging.", "Carol Chen",
                       f"{DEMO_URL}/browse/PLAT-3"):
            assert needle in txt, needle

        # Transition
        await pilot.press("t")
        await pick_single(app, pilot, "Review")
        await wait_for(pilot, lambda: scr.issue.status.name == "In Review", msg="transitioned")
        # Comment
        await pilot.press("n")
        await wait_for(pilot, lambda: isinstance(app.screen, TextPromptScreen))
        app.screen.query_one(TextArea).insert("Looks *good*")
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: fake.comments_posted == [("PLAT-3", "Looks *good*")])
        await wait_for(pilot, lambda: "Comments (3)" in text_of(body))
        # Assign
        await pilot.press("a")
        await pick_single(app, pilot, "mary")
        await wait_for(pilot, lambda: scr.issue.assignee and scr.issue.assignee.name == "mary ann", msg="assigned")
        # Go to sub-task
        await pilot.press("g")
        await pick_single(app, pilot, "PLAT-4")
        await wait_for(pilot, lambda: isinstance(app.screen, IssueDetailScreen) and app.screen.key == "PLAT-4")
        await pilot.press("escape")
        await wait_for(pilot, lambda: app.screen is scr)
        # Back on the list: it reloads because issues changed.
        await pilot.press("escape")
        await wait_for(pilot, lambda: app.screen is main)
        await wait_for(pilot, lambda: not pane.table.is_loading and pane.table.items[0].status.name == "In Review")


# -------------------------------------------------------------------- create
async def test_create_issue_flow(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot)
        await sprints_ready(app, pilot)
        await pilot.press("ctrl+n")
        await wait_for(pilot, lambda: any(isinstance(s, CreateIssueScreen) for s in app.screen_stack))
        scr = next(s for s in app.screen_stack if isinstance(s, CreateIssueScreen))
        await pick_single(app, pilot, "Bug")  # the type picker opens automatically for the current project
        await wait_for(pilot, lambda: scr.editors and app.screen is scr, msg="form built")
        ids = [e.field.field_id for e in scr.editors]
        assert ids[:9] == ["summary", "description", "assignee", "priority", "labels", "components",
                           "customfield_10002", "customfield_10003", "customfield_10001"]
        assert "customfield_10005" in ids and "reporter" not in ids  # required w/o default vs has default

        # Local required-field validation.
        await pilot.press("ctrl+s")
        status = scr.query_one("#c-status", Static)
        await wait_for(pilot, lambda: "Please fix" in text_of(status))
        for f in ("Summary", "Priority", "Team"):
            assert f in text_of(status)
        assert "Summary is required." in text_of(scr.query_one("#c-err-summary", Static))
        assert fake.created == []

        # Fill in; the epic link points at a non-epic -> Jira's field error shown inline.
        scr.query_one("#c-f-summary", Input).value = "Crash when saving [iOS]"
        scr.query_one("#c-f-description", TextArea).insert("Steps:\n# open\n# save")
        scr.query_one("#c-f-priority", Select).value = "2"
        scr.query_one("#c-f-customfield_10005", Select).value = "20002"
        scr.query_one("#c-f-labels", Input).value = "backend, ux/ui"
        scr.query_one("#c-f-customfield_10002", Input).value = "3"
        scr.query_one("#c-f-customfield_10003", Input).value = "plat-3"
        scr.query_one("#c-f-customfield_10001").focus()  # sprint picker button
        await pilot.press("enter")
        await pick_single(app, pilot, "Sprint 13")
        scr.query_one("#c-f-assignee").focus()
        await pilot.press("enter")
        await pick_single(app, pilot, "carol")
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: "Jira rejected" in text_of(status), msg="server field error")
        assert "not an epic" in text_of(scr.query_one("#c-err-customfield_10003", Static))

        scr.query_one("#c-f-customfield_10003", Input).value = "PLAT-1"
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: "Created PLAT-112" in text_of(status), msg="created")
        sent = fake.created[-1]["fields"]
        assert sent["project"] == {"key": "PLAT"} and sent["issuetype"] == {"id": "10004"}
        assert sent["priority"] == {"id": "2"} and sent["customfield_10005"] == {"id": "20002"}
        assert sent["labels"] == ["backend", "ux/ui"] and sent["customfield_10002"] == 3.0
        assert sent["customfield_10001"] == 13 and sent["assignee"] == {"name": "carol"}
        assert sent["customfield_10003"] == "PLAT-1"

        await pilot.press("ctrl+o")
        await wait_for(pilot, lambda: isinstance(app.screen, IssueDetailScreen) and app.screen.key == "PLAT-112")
        body = app.screen.query_one("#issue-body", Static)
        await wait_for(pilot, lambda: "Crash when saving [iOS]" in text_of(body), msg="new issue opened")
        assert "PLAT Sprint 13" in text_of(body) and "Carol Chen" in text_of(body)


async def test_create_with_legacy_createmeta_and_subtask_parent():
    fake = FakeJira(new_createmeta=False)
    app = make_app(fake, last_project="MOB")
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot)
        app.push_screen(CreateIssueScreen("MOB"))
        await wait_for(pilot, lambda: isinstance(app.screen, PickerScreen))
        await pick_single(app, pilot, "Sub-task")
        scr = app.screen
        await wait_for(pilot, lambda: isinstance(scr, CreateIssueScreen) and scr.editors, msg="form")
        assert "legacy createmeta" in text_of(scr.query_one("#c-status")) or app.client.createmeta_mode == "legacy"
        scr.query_one("#c-f-summary", Input).value = "Child"
        scr.query_one("#c-f-parent", Input).value = "not a key"
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: "issue key" in text_of(scr.query_one("#c-err-parent", Static)))
        scr.query_one("#c-f-parent", Input).value = "MOB-2"
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: "Created MOB-6" in text_of(scr.query_one("#c-status")), msg="created")
        assert fake.created[-1]["fields"]["parent"] == {"key": "MOB-2"}


# ---------------------------------------------------------- never spin forever
async def test_escape_cancels_slow_request_and_r_retries(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        main = await login(app, pilot)
        await sprints_ready(app, pilot)
        gate = asyncio.Event()
        real = app.client.search

        async def slow(*a, **k):
            await gate.wait()
            return await real(*a, **k)

        app.client.search = slow
        await pilot.press("f4")
        pane = main.query_one(IssuesPane)
        await wait_for(pilot, lambda: pane.table.is_loading, msg="loading")
        await wait_for(pilot, lambda: "s  (Esc to cancel" in text_of(pane.table.status), msg="elapsed time shown")
        await pilot.press("escape")
        await wait_for(pilot, lambda: not pane.table.is_loading and "Cancelled" in text_of(pane.table.status))
        assert app.screen is main  # first Esc cancels, does not navigate
        app.client.search = real
        pane.table.focus_table()
        await pilot.press("r")
        await wait_for(pilot, lambda: pane.table.table.row_count > 0, msg="retry worked")


async def test_ui_timeout_surfaces_error(fake):
    app = make_app(fake)
    app.ui_timeout = 0.5
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot)
        pane = await sprints_ready(app, pilot)

        async def hang(*a, **k):
            await asyncio.sleep(30)

        app.client.search = hang
        pane.table.focus_table()
        await pilot.press("r")
        await wait_for(pilot, lambda: "gave up" in text_of(pane.table.status), timeout=5, msg="timeout error")
        assert "retry" in text_of(pane.table.status)


async def test_server_error_shows_message_and_retry(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot)
        pane = await sprints_ready(app, pilot)
        fake.fail_next.append((r"/search$", 500))  # 500 is not retried automatically (502/503/504/429 are)
        pane.table.focus_table()
        await pilot.press("r")
        await wait_for(pilot, lambda: "HTTP 500" in text_of(pane.table.status), msg="500 shown")
        assert "Press r to retry" in text_of(pane.table.status)
        await pilot.press("r")
        await wait_for(pilot, lambda: pane.table.table.row_count == 26, msg="recovered")


async def test_demo_autoconnect_and_logout():
    fake = FakeJira()
    app = make_app(fake, secret=DEMO_TOKEN, auto_connect=True)
    async with app.run_test(size=SIZE) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, MainScreen), msg="auto connect")
        await sprints_ready(app, pilot)
        await pilot.press("escape")  # asks first
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("n")
        await wait_for(pilot, lambda: isinstance(app.screen, MainScreen) and app.client is not None)
        await pilot.press("escape")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await wait_for(pilot, lambda: isinstance(app.screen, LoginScreen) and app.client is None, msg="logged out")
