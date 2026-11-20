import json
import os
import stat

from jiratui import config
from jiratui.filters import GLOBAL, PROJECT, CustomFilter, FilterStore, QueryStore
from jiratui.api.models import JiraFilter


def test_config_dir_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("JIRATUI_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.config_dir() == str(tmp_path / "xdg" / "jira-tui")
    store = FilterStore()
    assert store.path == str(tmp_path / "xdg" / "jira-tui" / "filters.json")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert config.config_dir() == os.path.expanduser("~/.config/jira-tui")


def test_custom_filter_fragment():
    f = CustomFilter(name="x", statuses=["To Do", "In Progress"], issue_types=["Bug"], priorities=[],
                     text='say "hi"', jql="priority = High OR priority = Highest ORDER BY key")
    assert f.fragment() == ('status in ("To Do", "In Progress") AND issuetype = "Bug" AND text ~ "say \\"hi\\"" '
                            "AND (priority = High OR priority = Highest)")
    assert CustomFilter(name="empty").is_empty


def test_filter_store_roundtrip_scoping_and_permissions(tmp_path):
    path = str(tmp_path / "cfg" / "filters.json")
    store = FilterStore(path)
    assert store.filters == []
    plat = CustomFilter(name="Zed PLAT", jql="priority = High", project_key="PLAT", scope=PROJECT)
    plat2 = CustomFilter(name="alpha PLAT", statuses=["Done"], project_key="PLAT")
    mob = CustomFilter(name="MOB only", jql="status = Done", project_key="MOB")
    glob = CustomFilter(name="Everywhere", text="crash", scope=GLOBAL)
    for f in (plat, plat2, mob, glob):
        store.upsert(f)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700

    again = FilterStore(path)
    assert [f.name for f in again.for_project("PLAT")] == ["alpha PLAT", "Zed PLAT", "Everywhere"]
    assert [f.name for f in again.for_project("plat")] == ["alpha PLAT", "Zed PLAT", "Everywhere"]
    assert [f.name for f in again.for_project("MOB")] == ["MOB only", "Everywhere"]
    # edit keeps the id; delete removes
    edited = again.get(plat.id)
    edited.name = "Renamed"
    again.upsert(edited)
    assert FilterStore(path).get(plat.id).name == "Renamed"
    assert again.delete(mob.id) and not again.delete("nope")
    data = json.loads(open(path).read())
    assert data["version"] == 1 and len(data["filters"]) == 3


def test_filter_store_ignores_corrupt_file(tmp_path):
    path = tmp_path / "filters.json"
    path.write_text("{not json")
    assert FilterStore(str(path)).filters == []


def test_jira_favourites_are_read_only(tmp_path):
    fav = CustomFilter.from_jira(JiraFilter(id="10100", name="Mine", jql="assignee = currentUser()"))
    assert fav.read_only and fav.scope_label() == "Jira favourite"
    store = FilterStore(str(tmp_path / "f.json"))
    try:
        store.upsert(fav)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("favourites must not be stored locally")


def test_query_store_history_and_saved(tmp_path):
    path = str(tmp_path / "queries.json")
    qs = QueryStore(path)
    for q in ["a = 1", "b = 2", "a = 1", "  ", "c = 3"]:
        qs.add_history(q)
    assert qs.history == ["c = 3", "a = 1", "b = 2"]
    for i in range(150):
        qs.add_history(f"q{i}")
    assert len(qs.history) == QueryStore.MAX_HISTORY and qs.history[0] == "q149"
    qs.save_query("My bugs", "issuetype = Bug")
    qs.save_query("All", "project = X")
    qs.save_query("My bugs", "issuetype = Bug AND assignee = currentUser()")
    again = QueryStore(path)
    assert [(s.name, s.jql) for s in again.saved] == [("All", "project = X"),
                                                      ("My bugs", "issuetype = Bug AND assignee = currentUser()")]
    again.delete_query("All")
    assert [s.name for s in QueryStore(path).saved] == ["My bugs"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
