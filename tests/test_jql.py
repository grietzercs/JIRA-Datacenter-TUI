import pytest

from jiratui import jql
from jiratui.filters import CustomFilter, FilterState
from jiratui.testing import fake_jql
from jiratui.testing.fake_server import FakeJira


# ------------------------------------------------------------------ quoting
@pytest.mark.parametrize(
    "value,expected",
    [
        ("backend", '"backend"'),
        ("ux/ui", '"ux/ui"'),
        ("release:2.0", '"release:2.0"'),
        ("o'reilly", '"o\'reilly"'),
        ('say "hi"', '"say \\"hi\\""'),
        ("back\\slash", '"back\\\\slash"'),
        ("mary ann", '"mary ann"'),
        ("j.smith@corp", '"j.smith@corp"'),
        ("line\nbreak", '"line\\nbreak"'),
        ("AND", '"AND"'),
    ],
)
def test_quote_value(value, expected):
    assert jql.quote_value(value) == expected


@pytest.mark.parametrize(
    "name,expected",
    [("labels", "labels"), ("customfield_10002", "cf[10002]"), ("cf[10002]", "cf[10002]"),
     ("Story Points", '"Story Points"'), ("order", '"order"'), ("issuetype", "issuetype")],
)
def test_field_ref(name, expected):
    assert jql.field_ref(name) == expected


def test_clauses():
    assert jql.labels_clause([]) == ""
    assert jql.labels_clause(["a b", 'x"y']) == 'labels in ("a b", "x\\"y")'
    assert jql.in_clause("status", ["Done"]) == 'status = "Done"'
    assert jql.in_clause("status", ["To Do", "In Progress"]) == 'status in ("To Do", "In Progress")'
    assert jql.in_clause("customfield_10002", ["3"]) == 'cf[10002] = "3"'
    assert jql.text_clause("  login page ") == 'text ~ "login page"'
    assert jql.text_clause("") == ""


def test_people_clause_combinations():
    assert jql.people_clause([]) == ""
    assert jql.people_clause(["mary ann"]) == 'assignee in ("mary ann")'
    assert jql.people_clause([jql.UNASSIGNED]) == "assignee is EMPTY"
    assert jql.people_clause(["alice", jql.UNASSIGNED, "j.smith@corp"]) == \
        '(assignee in ("alice", "j.smith@corp") OR assignee is EMPTY)'


# ----------------------------------------------------------------- order by
def test_split_order_by_is_quote_aware():
    assert jql.split_order_by("project = X ORDER BY updated DESC") == ("project = X", "updated DESC")
    assert jql.split_order_by('summary ~ "order by" ORDER BY key') == ('summary ~ "order by"', "key")
    assert jql.split_order_by('summary ~ "order by"') == ('summary ~ "order by"', "")
    assert jql.split_order_by("order by rank") == ("", "rank")
    assert jql.split_order_by("project = X order\n  by created") == ("project = X", "created")


def test_set_order_by():
    assert jql.set_order_by("project = X ORDER BY key", "updated", "desc") == "project = X ORDER BY updated DESC"
    assert jql.set_order_by("", "Story Points", "ASC") == 'ORDER BY "Story Points" ASC'


# ---------------------------------------------------------------- combining
def test_and_join_wraps_or_and_not_and_drops_order_by():
    assert jql.and_join(["a = 1", "b = 2 OR c = 3", "", "NOT d = 4", "e = 5 ORDER BY x"]) == \
        "a = 1 AND (b = 2 OR c = 3) AND (NOT d = 4) AND e = 5"
    # OR inside parentheses or quotes is not top-level
    assert jql.and_join(['(b = 2 OR c = 3)', 'summary ~ "this OR that"']) == \
        '(b = 2 OR c = 3) AND summary ~ "this OR that"'


def test_compose_keeps_base_order_unless_overridden():
    assert jql.compose("project = X ORDER BY key", ["labels in (\"a\")"]) == \
        'project = X AND labels in ("a") ORDER BY key'
    assert jql.compose("sprint = 12", [], "Rank ASC") == "sprint = 12 ORDER BY Rank ASC"
    assert jql.compose("", ["priority = High OR priority = Highest"]) == "(priority = High OR priority = Highest)"


def test_filter_state_combination():
    st = FilterState(labels=["ux/ui", "backend"], people=["alice", jql.UNASSIGNED],
                     custom=CustomFilter(name="c", jql="priority = High OR priority = Highest",
                                         statuses=["In Progress"], text="login"))
    q = jql.compose(jql.sprint_clause(12), st.fragments(), "Rank ASC")
    assert q == ('sprint = 12 AND labels in ("ux/ui", "backend") AND (assignee in ("alice") OR assignee is EMPTY) '
                 'AND status = "In Progress" AND text ~ "login" AND (priority = High OR priority = Highest) '
                 "ORDER BY Rank ASC")
    st.clear()
    assert st.is_empty and jql.compose(jql.sprint_clause(12), st.fragments()) == "sprint = 12"


def test_backlog_base():
    assert jql.backlog_base("PLAT") == ('project = "PLAT" AND (sprint is EMPTY OR sprint in closedSprints()) '
                                        "AND statusCategory != Done")


# -------------------------------------------- round trip through the fake JQL
@pytest.fixture
def fj():
    return FakeJira()


def keys(fj, q):
    return [i["key"] for i in fj._run_jql(q)]


def test_generated_jql_runs_on_fake_server(fj):
    st = FilterState(labels=["ux/ui"], people=["alice", jql.UNASSIGNED])
    q = jql.compose(jql.sprint_clause(12), st.fragments(), "Rank ASC")
    got = keys(fj, q)
    assert got and got[0] == "PLAT-3"
    for k in got:
        issue = fj.issue(k)
        assert "ux/ui" in issue["fields"]["labels"] and 12 in issue["_sprints"]
        assert issue["fields"]["assignee"] is None or issue["fields"]["assignee"]["name"] == "alice"


def test_special_characters_round_trip(fj):
    assert keys(fj, jql.compose("", [jql.people_clause(["mary ann"])])) == \
        [i["key"] for i in fj.issues if (i["fields"]["assignee"] or {}).get("name") == "mary ann"]
    assert "PLAT-7" in keys(fj, jql.compose("", [jql.people_clause(["j.smith@corp"])]))
    assert "PLAT-7" in keys(fj, jql.compose("", [jql.labels_clause(["o'reilly"])]))
    assert "PLAT-6" in keys(fj, jql.compose("", [jql.labels_clause(["release:2.0"])]))
    assert keys(fj, jql.compose("", [jql.labels_clause(['no "such" label'])])) == []


def test_fake_jql_features(fj):
    assert keys(fj, "key = PLAT-3") == ["PLAT-3"]
    assert keys(fj, 'project = MOB AND status != Done ORDER BY key DESC') == ["MOB-4", "MOB-3", "MOB-2", "MOB-1"]
    assert keys(fj, "project = OPS AND NOT assignee = carol") == ["OPS-2"]
    assert keys(fj, 'project = MOB AND sprint in openSprints() ORDER BY "Story Points" DESC') == \
        ["MOB-3", "MOB-2", "MOB-1"]
    assert keys(fj, "project = LEG AND assignee = currentUser()") == ["LEG-1"]
    assert keys(fj, 'text ~ "expired session"') == ["PLAT-3"]
    assert keys(fj, "project = PLAT AND cf[10002] >= 8") == ["PLAT-5"]
    assert set(keys(fj, "project = OPS AND resolution = Unresolved")) == {"OPS-1", "OPS-2"}


@pytest.mark.parametrize(
    "q,fragment",
    [
        ("project = PLAT AND", "Expecting a field name but got end of query"),
        ("project PLAT", "Expecting operator"),
        ('summary ~ "unterminated', "has not been completed"),
        ("status ~ Done", "operator '~' is not supported"),
        ("bogus = 1", "Field 'bogus' does not exist"),
        ("project = PLAT ORDER BY bogus", "Not able to sort using field 'bogus'"),
        ("assignee in (alice", "Expecting ')'"),
        ("sprint in nosuchFunction()", "Unable to find JQL function 'nosuchfunction()'"),
    ],
)
def test_fake_jql_errors(fj, q, fragment):
    with pytest.raises(fake_jql.JqlSyntaxError) as ei:
        fj._run_jql(q)
    assert fragment in " ".join(ei.value.messages)
