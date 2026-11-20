"""Issue table columns/rows shared by the Sprints, Backlog and Issues views."""

from __future__ import annotations

from typing import Sequence

from rich.text import Text

from ..api.models import Issue, format_points, format_ts
from .common import PagedTable, cell

ISSUE_COLUMNS = ["Key", "Type", "Summary", "Status", "Assignee", "Priority", "SP", "Labels"]
SEARCH_COLUMNS = ISSUE_COLUMNS + ["Updated"]

CATEGORY_STYLE = {"new": "", "indeterminate": "bold yellow", "done": "bold green"}
PRIORITY_STYLE = {"Highest": "bold red", "High": "red", "Medium": "", "Low": "dim", "Lowest": "dim"}


def status_text(issue: Issue) -> Text:
    return cell(issue.status.name, CATEGORY_STYLE.get(issue.status.category_key, ""))


def issue_row(issue: Issue) -> Sequence[Text]:
    summary = issue.summary if len(issue.summary) <= 80 else issue.summary[:79] + "…"
    assignee = cell(issue.assignee.display_name, "") if issue.assignee else cell("Unassigned", "dim italic")
    prio = issue.priority.name if issue.priority else ""
    return (
        cell(issue.key, "bold"),
        cell(issue.issue_type.name),
        cell(summary),
        status_text(issue),
        assignee,
        cell(prio, PRIORITY_STYLE.get(prio, "")),
        cell(format_points(issue.story_points)),
        cell(", ".join(issue.labels)),
    )


def search_row(issue: Issue) -> Sequence[Text]:
    return tuple(issue_row(issue)) + (cell(format_ts(issue.updated), "dim"),)


class IssueTable(PagedTable):
    """A PagedTable of issues; Enter opens the issue (handled by MainScreen)."""

    DEFAULT_CSS = """
    IssueTable { height: 1fr; }
    """
