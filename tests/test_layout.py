from rich.text import Text
from textual.widgets import DataTable

from jiratui.tui.common import fit_widths
from jiratui.tui.boards import BacklogPane

from tests.test_tui import login, make_app, wait_for


def test_fit_widths_unchanged_when_everything_fits():
    assert fit_widths([10, 40, 20], [1, 2], 80) is None


def test_fit_widths_shrinks_wrapping_columns_first():
    widths = fit_widths([10, 80, 60], [1, 2], 100)
    assert widths.keys() == {1, 2}
    assert 10 + sum(widths.values()) <= 100


def test_fit_widths_shrinks_every_column_when_wrapping_columns_are_not_enough():
    widths = fit_widths([20, 40, 30], [1], 40)
    assert widths.keys() == {0, 1, 2}
    assert sum(widths.values()) <= 40


async def test_issue_table_fits_narrow_window(fake):
    app = make_app(fake)
    async with app.run_test(size=(80, 30)) as pilot:
        main = await login(app, pilot)
        await pilot.press("f3")
        pane = main.query_one(BacklogPane)
        await wait_for(pilot, lambda: pane.table.table.row_count > 0 and not pane.table.is_loading)
        table = pane.table.table
        await wait_for(pilot, lambda: pane.table._widths is not None, msg="table fitted")
        await pilot.pause(0.2)
        assert table.virtual_size.width <= table.size.width


def _keys(pane):
    t = pane.table.table
    return [r.value for r in t.rows]


async def test_sort_cycle_orders_rows_and_labels_header(fake):
    app = make_app(fake)
    async with app.run_test(size=(160, 30)) as pilot:
        main = await login(app, pilot)
        await pilot.press("f3")
        pane = main.query_one(BacklogPane)
        pt = pane.table
        await wait_for(pilot, lambda: pt.table.row_count > 0 and not pt.is_loading)
        items = pt.items
        key_col = pt.columns.index("Key")
        pt.sort_by(key_col, True)
        await pilot.pause()
        groups = {}
        label = None
        for k in _keys(pane):
            if k.startswith("group-"):
                label = k
            else:
                groups.setdefault(label, []).append(pt.items[int(k)].key)
        number = lambda k: int(k.rpartition("-")[2])
        assert len(items) > 1
        assert all(keys == sorted(keys, key=number, reverse=True) for keys in groups.values())
        assert str(pt.table.ordered_columns[key_col].label).endswith("↓")
        pt.sort_by(None)
        await pilot.pause()
        assert str(pt.table.ordered_columns[key_col].label) == "Key"


async def test_sort_steps_through_every_column(fake):
    app = make_app(fake)
    async with app.run_test(size=(160, 30)) as pilot:
        main = await login(app, pilot)
        await pilot.press("f3")
        pt = main.query_one(BacklogPane).table
        await wait_for(pilot, lambda: pt.table.row_count > 0 and not pt.is_loading)
        seen = []
        for _ in range(len(pt.columns) * 2):
            pt._step_sort(1)
            seen.append(pt._sort)
        assert seen == [(c, d) for c in range(len(pt.columns)) for d in (False, True)]
        pt._step_sort(1)
        assert pt._sort is None


async def test_sort_by_header_click_and_keys(fake):
    app = make_app(fake)
    async with app.run_test(size=(160, 30)) as pilot:
        main = await login(app, pilot)
        await pilot.press("f3")
        pt = main.query_one(BacklogPane).table
        await wait_for(pilot, lambda: pt.table.row_count > 0 and not pt.is_loading)
        pt.table.focus()
        await pilot.press("right_square_bracket")
        assert pt._sort == (0, False)
        await pilot.press("left_square_bracket")
        assert pt._sort is None
        await pilot.press("left_square_bracket")
        assert pt._sort == (len(pt.columns) - 1, True)
        pt.table.post_message(DataTable.HeaderSelected(pt.table, pt.table.ordered_columns[2].key, 2, label=Text("x")))
        await pilot.pause()
        assert pt._sort == (2, False)
