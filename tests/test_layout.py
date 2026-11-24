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
