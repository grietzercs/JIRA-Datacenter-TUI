"""Shared TUI building blocks.

The key pieces are:

* :class:`PagedTable`: a DataTable backed by a lazy
  :class:`~jiratui.api.paging.Pager`. Every load runs in a worker with a hard
  timeout, shows elapsed time, can be cancelled (Esc) and shows an error with
  a retry key. Nothing is allowed to spin forever.
* :class:`PickerScreen`: a filterable single/multi-select modal whose options
  may load asynchronously under the same rules. Projects, boards, labels,
  people, statuses, issue types, history... all use it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, TypeVar, Union

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import Button, DataTable, Input, Label, OptionList, SelectionList, Static, TextArea
from textual.widgets.data_table import CellDoesNotExist, RowDoesNotExist
from textual.widgets.option_list import Option
from textual.worker import Worker

from ..api.errors import JiraError, TimeoutError_
from ..api.models import Page
from ..api.paging import Pager
from ..redact import redact

T = TypeVar("T")

Fetch = Callable[[int, int, str], Awaitable[Page[Any]]]
RowFn = Callable[[Any], Sequence[Any]]
Choice = Tuple[str, str]  # (value, label)
ChoiceSource = Union[Sequence[Choice], Callable[[], Awaitable[Sequence[Choice]]]]


def error_text(exc: BaseException) -> str:
    """User-facing, redacted description of any exception."""
    if isinstance(exc, JiraError):
        return redact(exc.describe())
    describe = getattr(exc, "describe", None)
    if callable(describe):
        return redact(str(describe()))
    if isinstance(exc, asyncio.TimeoutError):
        return "Timed out waiting for the server."
    return redact(f"{type(exc).__name__}: {exc}")


async def guarded(coro: Awaitable[T], timeout: float) -> T:
    """Await ``coro`` with an overall deadline mapped to a friendly error."""
    try:
        return await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        raise TimeoutError_(f"No complete response after {timeout:.0f}s; gave up.") from None


def ui_timeout(widget: Any) -> float:
    return float(getattr(widget.app, "ui_timeout", 60.0))


def cell(value: Any, style: str = "") -> Text:
    """DataTable cells: never interpret Jira text (``[iOS] crash``) as markup."""
    return Text("" if value is None else str(value), style=style, no_wrap=True, overflow="ellipsis")


SORT_ARROWS = {False: "↑", True: "↓"}
MIN_FLEX_WIDTH = 12
MIN_COLUMN_WIDTH = 4


def fit_widths(natural: Sequence[int], flex: Sequence[int], available: int) -> Optional[Dict[int, int]]:
    """Column widths that fit ``available`` cells, or ``None`` when everything fits at natural
    width. The ``flex`` columns shrink first; when that is not enough every column shrinks."""
    if not flex or sum(natural) <= available:
        return None
    room = available - sum(w for i, w in enumerate(natural) if i not in flex)
    if room >= MIN_FLEX_WIDTH * len(flex):
        flex_total = sum(natural[i] for i in flex)
        return {i: max(MIN_FLEX_WIDTH, min(natural[i], room * natural[i] // flex_total)) for i in flex}
    total = sum(natural)
    return {i: max(MIN_COLUMN_WIDTH, min(w, available * w // total)) for i, w in enumerate(natural)}


class LazyDataTable(DataTable):
    """DataTable that asks its PagedTable for more rows when scrolled near the end
    (mouse wheel, PgDn, Ctrl+End), not only when the cursor moves."""

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self.max_scroll_y and new_value >= self.max_scroll_y - 3:
            parent = self.parent
            if isinstance(parent, PagedTable):
                parent.load_more()


class PagedTable(Vertical):
    """Lazily-paged, optionally server-side-filtered table."""

    DEFAULT_CSS = """
    PagedTable { height: 1fr; }
    PagedTable > Input { margin: 0 0 0 0; }
    PagedTable > DataTable { height: 1fr; }
    PagedTable > .status { height: auto; max-height: 8; color: $text-muted; padding: 0 1; }
    PagedTable > .status.error { color: $error; }
    PagedTable > .status.loading { color: $warning; }
    """

    BINDINGS = [
        Binding("m", "load_more", "More", show=False),
        Binding("r", "retry", "Reload/Retry"),
        Binding("right_square_bracket", "sort_next", "Sort", show=False),
        Binding("left_square_bracket", "sort_prev", "Sort back", show=False),
    ]

    class Selected(Message):
        def __init__(self, table: "PagedTable", item: Any) -> None:
            super().__init__()
            self.table = table
            self.item = item

        @property
        def control(self) -> "PagedTable":
            return self.table

    class Highlighted(Message):
        # Deliberately NOT a subclass of Selected: Textual dispatches handlers
        # along the message MRO, so subclassing would "open" on highlight.
        def __init__(self, table: "PagedTable", item: Any) -> None:
            super().__init__()
            self.table = table
            self.item = item

        @property
        def control(self) -> "PagedTable":
            return self.table

    def __init__(
        self,
        columns: Sequence[str],
        fetch: Fetch,
        row: RowFn,
        *,
        limit: int = 50,
        filter_placeholder: Optional[str] = None,
        autoload: bool = True,
        empty_text: str = "Nothing here.",
        noun: str = "item",
        group: Optional[Callable[[Any], str]] = None,
        flex: Sequence[str] = (),
        sort: Optional[Dict[str, Callable[[Any], Any]]] = None,
        id: Optional[str] = None,
    ) -> None:
        super().__init__(id=id)
        self.columns = list(columns)
        self._fetch = fetch
        self._row = row
        self.limit = limit
        self.filter_placeholder = filter_placeholder
        self.autoload = autoload
        self.empty_text = empty_text
        self.noun = noun
        self.group = group
        self.flex = [self.columns.index(name) for name in flex]  # columns that wrap to fit the window
        self._sort_keys = sort or {}  # column name -> key(item); other columns sort by cell text
        self._sort: Optional[Tuple[int, bool]] = None  # (column index, descending)
        self._natural = [len(self._label(i)) for i in range(len(self.columns))]
        self._widths: Optional[Dict[int, int]] = None
        self._last_group: Optional[str] = None
        self._fit_timer: Optional[Timer] = None
        self.filter_text = ""
        self.pager: Pager[Any] = self._new_pager()
        self.is_loading = False
        self.failed = False
        self.loaded_once = False
        self._gen = 0
        self._worker: Optional[Worker] = None
        self._tick_timer: Optional[Timer] = None
        self._filter_timer: Optional[Timer] = None
        self._t0 = 0.0

    def _label(self, i: int) -> str:
        if self._sort is None or self._sort[0] != i:
            return self.columns[i]
        return f"{self.columns[i]} {SORT_ARROWS[self._sort[1]]}"

    def _new_pager(self) -> Pager[Any]:
        flt = self.filter_text
        fetch = self._fetch
        return Pager(lambda start, limit: fetch(start, limit, flt), limit=self.limit)

    def compose(self) -> ComposeResult:
        if self.filter_placeholder:
            yield Input(placeholder=self.filter_placeholder, classes="filter")
        yield LazyDataTable(cursor_type="row", zebra_stripes=True)
        yield Static("", classes="status")

    @property
    def table(self) -> DataTable:
        return self.query_one(DataTable)

    @property
    def status(self) -> Static:
        return self.query_one(".status", Static)

    @property
    def items(self) -> List[Any]:
        return self.pager.items

    def on_mount(self) -> None:
        self.table.add_columns(*(self._label(i) for i in range(len(self.columns))))
        if self.autoload:
            self.reload()

    def ensure_loaded(self) -> None:
        if not self.loaded_once and not self.is_loading:
            self.reload()

    def set_fetch(self, fetch: Fetch, reload: bool = True) -> None:
        self._fetch = fetch
        if reload:
            self.reload()

    def show_message(self, text: str, error: bool = False) -> None:
        """Clear the table and show a message (e.g. 'pick a project first')."""
        self.cancel(silent=True)
        self._gen += 1
        self.pager = self._new_pager()
        self.pager.next_start = None
        self.table.clear()
        self._set_status(text, "error" if error else "")

    # ----------------------------------------------------------- loading
    def reload(self) -> None:
        self.cancel(silent=True)
        self._gen += 1
        self.pager = self._new_pager()
        self.failed = False
        self._last_group = None
        self._natural = [len(self._label(i)) for i in range(len(self.columns))]
        self.table.clear()
        self.loaded_once = True
        self.load_more()

    def load_more(self) -> None:
        if self.is_loading or self.pager.exhausted:
            return
        # Set synchronously: several triggers (scroll + cursor) can fire in the
        # same tick, before the worker starts running.
        self.is_loading = True
        gen = self._gen
        self._worker = self.run_worker(self._load(gen), group="paged", exit_on_error=False)

    async def _load(self, gen: int) -> None:
        pager = self.pager
        self.failed = False
        self._t0 = time.monotonic()
        self._set_status(f"Loading page {pager.pages_loaded + 1}…  (Esc to cancel)", "loading")
        self._tick_timer = self.set_interval(0.25, self._tick)
        try:
            new = await guarded(pager.load_next(), ui_timeout(self))
        except asyncio.CancelledError:
            if gen == self._gen:
                self._set_status("Cancelled. Press r to retry.", "error")
                self.failed = True
            raise
        except Exception as exc:  # noqa: BLE001 - everything must surface
            if gen == self._gen:
                self.failed = True
                self._set_status(f"{error_text(exc)}\nPress r to retry.", "error")
            return
        finally:
            if gen == self._gen:
                self.is_loading = False
                if self._tick_timer:
                    self._tick_timer.stop()
                    self._tick_timer = None
        if gen != self._gen:
            return
        base = len(pager.items) - len(new)
        rows = self._rows(range(base, len(pager.items)))
        for cells in rows.values():
            for i, cell_ in enumerate(cells):
                self._natural[i] = max(self._natural[i], cell_.cell_len)
        if self._sort is not None or self._fit() != self._widths:
            self._rebuild()
        else:
            for key, cells in rows.items():
                self.table.add_row(*cells, key=key, height=None if self._widths else 1)
        self._update_done_status()
        if base == 0 and new:
            if self.group is not None:
                self.table.move_cursor(row=1)  # skip the first group header
            self.post_message(self.Highlighted(self, new[0]))

    def _rows(self, order: Iterable[int]) -> Dict[str, List[Text]]:
        """Row cells by row key (the item index), with a header row whenever the group changes.
        Columns narrowed to fit the window get cells that wrap; the rest stay on one line."""
        out: Dict[str, List[Text]] = {}
        for i in order:
            item = self.pager.items[i]
            if self.group is not None and (label := self.group(item)) != self._last_group:
                self._last_group = label
                header = [cell("", "reverse") for _ in self.columns]
                header[(self.flex or [0])[0]] = cell(label, "bold reverse")
                out[f"group-{i}"] = header
            out[str(i)] = [c if isinstance(c, Text) else cell(c) for c in self._row(item)]
        for cells in out.values():
            for i in self._widths or ():
                cells[i].no_wrap, cells[i].overflow = False, "fold"
        return out

    def _order(self) -> List[int]:
        """Item indexes in display order: sorted within each group, empty values last."""
        if self._sort is None:
            return list(range(len(self.pager.items)))
        col, desc = self._sort
        name = self.columns[col]

        def key(i: int) -> Any:
            item = self.pager.items[i]
            if name in self._sort_keys:
                return self._sort_keys[name](item)
            c = self._row(item)[col]
            return (c.plain if isinstance(c, Text) else str(c)).casefold() or None

        groups: Dict[Optional[str], List[int]] = {}
        for i, item in enumerate(self.pager.items):
            groups.setdefault(self.group(item) if self.group else None, []).append(i)
        out: List[int] = []
        for idx in groups.values():
            keyed = [(key(i), i) for i in idx]
            out += [i for k, i in sorted((x for x in keyed if x[0] is not None), key=lambda x: x[0], reverse=desc)]
            out += [i for k, i in keyed if k is None]
        return out

    def _fit(self) -> Optional[Dict[int, int]]:
        t = self.table
        if not t.size.width:
            return None
        available = t.size.width - t.scrollbar_size_vertical - len(self.columns) * 2 * t.cell_padding
        return fit_widths(self._natural, self.flex, available)

    def _rebuild(self) -> None:
        """Re-add columns and rows at the widths that fit the current window."""
        t = self.table
        cursor = t.cursor_row
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value if t.row_count else None
        self._widths = self._fit()
        t.clear(columns=True)
        for i in range(len(self.columns)):
            t.add_column(self._label(i), width=(self._widths or {}).get(i))
        self._last_group = None
        for row_key, cells in self._rows(self._order()).items():
            t.add_row(*cells, key=row_key, height=None if self._widths else 1)
        if not t.row_count:
            return
        try:
            t.move_cursor(row=t.get_row_index(key) if key else min(cursor, t.row_count - 1), animate=False)
        except RowDoesNotExist:
            t.move_cursor(row=min(cursor, t.row_count - 1), animate=False)

    def on_resize(self) -> None:
        if self._fit_timer:
            self._fit_timer.stop()
        self._fit_timer = self.set_timer(0.1, self._refit)

    def _refit(self) -> None:
        if self._fit() != self._widths:
            self._rebuild()

    def sort_by(self, column: Optional[int], desc: bool = False) -> None:
        self._sort = None if column is None else (column, desc)
        if column is not None:
            self._natural[column] = max(self._natural[column], len(self._label(column)))
        self._rebuild()

    def action_sort_next(self) -> None:
        self._step_sort(1)

    def action_sort_prev(self) -> None:
        self._step_sort(-1)

    def _step_sort(self, step: int) -> None:
        """Cycle none, then each column ascending and descending."""
        state = 0 if self._sort is None else 1 + self._sort[0] * 2 + int(self._sort[1])
        state = (state + step) % (1 + len(self.columns) * 2)
        if state == 0:
            self.sort_by(None)
        else:
            self.sort_by((state - 1) // 2, bool((state - 1) % 2))

    @on(DataTable.HeaderSelected)
    def _header_selected(self, ev: DataTable.HeaderSelected) -> None:
        """Click a header: ascending, again for descending, again to clear."""
        ev.stop()
        col = ev.column_index
        if self._sort is None or self._sort[0] != col:
            self.sort_by(col)
        elif not self._sort[1]:
            self.sort_by(col, True)
        else:
            self.sort_by(None)

    def _tick(self) -> None:
        if self.is_loading:
            el = time.monotonic() - self._t0
            self._set_status(
                f"Loading page {self.pager.pages_loaded + 1}… {el:.1f}s  "
                f"(Esc to cancel, gives up after {ui_timeout(self):.0f}s)",
                "loading",
            )

    def _update_done_status(self) -> None:
        n = len(self.pager.items)
        total = self.pager.total
        flt = f" matching '{self.filter_text}'" if self.filter_text else ""
        of = f" of {total}" if total is not None and total != n else ""
        if n == 0:
            self._set_status(self.empty_text + (f" (filter: '{self.filter_text}')" if self.filter_text else ""))
        elif self.pager.exhausted:
            self._set_status(f"{n} {self.noun}(s){flt}, all loaded.")
        else:
            self._set_status(f"{n}{of} {self.noun}(s){flt} loaded; more available: scroll down or press m.")

    def _set_status(self, text: str, cls: str = "") -> None:
        st = self.status
        st.update(Text(text))
        st.set_class(cls == "error", "error")
        st.set_class(cls == "loading", "loading")

    def cancel(self, silent: bool = False) -> bool:
        if self.is_loading and self._worker is not None:
            # Clean up here (the worker may be cancelled before it ever ran) and
            # bump the generation so the cancelled coroutine's handlers are no-ops.
            self._gen += 1
            self.is_loading = False
            if self._tick_timer:
                self._tick_timer.stop()
                self._tick_timer = None
            self._worker.cancel()
            if not silent:
                self.failed = True
                self._set_status("Cancelled. Press r to retry.", "error")
            return True
        return False

    # ------------------------------------------------------------ actions
    def action_load_more(self) -> None:
        self.load_more()

    def action_retry(self) -> None:
        if self.failed and self.pager.items:
            self.load_more()
        else:
            self.reload()

    def current_item(self) -> Optional[Any]:
        t = self.table
        if t.row_count == 0:
            return None
        try:
            key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
            return self.pager.items[int(str(key))]
        except (ValueError, IndexError, TypeError, CellDoesNotExist):
            return None  # group header rows have no item

    @on(DataTable.RowHighlighted)
    def _highlighted(self, ev: DataTable.RowHighlighted) -> None:
        ev.stop()
        if ev.cursor_row >= self.table.row_count - 3:
            self.load_more()
        item = self.current_item()
        if item is not None:
            self.post_message(self.Highlighted(self, item))

    @on(DataTable.RowSelected)
    def _selected(self, ev: DataTable.RowSelected) -> None:
        ev.stop()
        try:
            item = self.pager.items[int(str(ev.row_key.value))]
        except (ValueError, IndexError, TypeError):
            return
        self.post_message(self.Selected(self, item))

    @on(Input.Changed, ".filter")
    def _filter_changed(self, ev: Input.Changed) -> None:
        ev.stop()
        if self._filter_timer:
            self._filter_timer.stop()
        value = ev.value.strip()
        self._filter_timer = self.set_timer(0.4, lambda: self._apply_filter(value))

    @on(Input.Submitted, ".filter")
    def _filter_submitted(self, ev: Input.Submitted) -> None:
        ev.stop()
        if self._filter_timer:
            self._filter_timer.stop()
        self._apply_filter(ev.value.strip(), force=True)
        self.table.focus()

    def _apply_filter(self, value: str, force: bool = False) -> None:
        if value == self.filter_text and not force:
            return
        self.filter_text = value
        self.reload()

    def focus_table(self) -> None:
        self.table.focus()


class BaseScreen(Screen):
    """Screen with Esc = cancel loading, else go back."""

    BINDINGS = [
        Binding("escape", "back", "Back/Cancel"),
    ]

    def cancel_loading(self) -> bool:
        cancelled = False
        for pt in self.query(PagedTable):
            cancelled = pt.cancel() or cancelled
        for w in list(getattr(self, "_api_workers", [])):
            if w.is_running:
                w.cancel()
                cancelled = True
        return cancelled

    def action_back(self) -> None:
        if self.cancel_loading():
            self.notify("Cancelled.", timeout=2)
            return
        if len(self.app.screen_stack) > 2:
            self.app.pop_screen()

    def run_api(self, coro: Awaitable[Any], group: str = "api", exclusive: bool = True) -> Worker:
        if not hasattr(self, "_api_workers"):
            self._api_workers = []  # type: ignore[attr-defined]
        w = self.run_worker(coro, group=group, exclusive=exclusive, exit_on_error=False)
        self._api_workers = [x for x in self._api_workers if x.is_running] + [w]  # type: ignore[attr-defined]
        return w


class ConfirmScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Vertical { width: 70; max-width: 100%; height: auto; border: thick $warning; background: $surface; padding: 1 2; }
    ConfirmScreen Horizontal { height: auto; margin-top: 1; }
    ConfirmScreen Button { margin-right: 2; }
    """
    BINDINGS = [Binding("y", "yes", "Yes"), Binding("n,escape", "no", "No")]

    def __init__(self, message: str, yes_label: str = "Yes", danger: bool = False) -> None:
        super().__init__()
        self.message = message
        self.yes_label = yes_label
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(self.message))
            with Horizontal():
                yield Button(f"{self.yes_label} (y)", variant="error" if self.danger else "primary", id="yes")
                yield Button("No (n)", id="no")

    @on(Button.Pressed, "#yes")
    def action_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def action_no(self) -> None:
        self.dismiss(False)


class TextPromptScreen(ModalScreen[Optional[str]]):
    """Multi-line (TextArea) or single-line (Input) text prompt."""

    DEFAULT_CSS = """
    TextPromptScreen { align: center middle; }
    TextPromptScreen > Vertical { width: 90; max-width: 100%; height: auto; max-height: 24; border: thick $primary; background: $surface; padding: 1 2; }
    TextPromptScreen TextArea { height: 12; }
    TextPromptScreen Horizontal { height: auto; margin-top: 1; }
    TextPromptScreen Button { margin-right: 2; }
    """
    BINDINGS = [Binding("ctrl+s", "submit", "Submit"), Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, initial: str = "", multiline: bool = True, placeholder: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.initial = initial
        self.multiline = multiline
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical():
            hint = "Ctrl+S to submit" if self.multiline else "Enter to submit"
            yield Label(Text(f"{self.title_text}  ({hint}, Esc to cancel)"))
            if self.multiline:
                yield TextArea(self.initial, id="text")
            else:
                yield Input(self.initial, placeholder=self.placeholder, id="text")
            with Horizontal():
                yield Button("Submit", variant="primary", id="submit")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#text").focus()

    def _value(self) -> str:
        w = self.query_one("#text")
        return w.text if isinstance(w, TextArea) else w.value  # type: ignore[union-attr]

    @on(Input.Submitted, "#text")
    @on(Button.Pressed, "#submit")
    def action_submit(self) -> None:
        text = self._value()
        self.dismiss(text if text.strip() else None)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class PickerScreen(ModalScreen[Optional[List[str]]]):
    """Filterable picker. Dismisses with the chosen values (a one-item list in
    single mode), or ``None`` when cancelled.

    ``options`` is a list of ``(value, label)`` pairs or an async callable
    returning one; async loads are bounded by the app's UI timeout, show elapsed
    time, and can be retried with Ctrl+R after an error.
    """

    DEFAULT_CSS = """
    PickerScreen { align: center middle; }
    PickerScreen > Vertical { width: 90; max-width: 100%; height: 80%; border: thick $primary; background: $surface; padding: 0 1; }
    PickerScreen #pick-title { text-style: bold; padding: 0 1; }
    PickerScreen #pick-list { height: 1fr; }
    PickerScreen #pick-status { height: auto; max-height: 6; color: $text-muted; padding: 0 1; }
    PickerScreen #pick-status.error { color: $error; }
    PickerScreen #pick-hint { color: $text-muted; padding: 0 1; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "apply", "Apply"),
        Binding("ctrl+r", "reload", "Retry", show=False),
        Binding("ctrl+x", "clear_selection", "Clear", show=False),
    ]

    def __init__(
        self,
        title: str,
        options: ChoiceSource,
        *,
        multi: bool = False,
        selected: Iterable[str] = (),
        pinned: Sequence[Choice] = (),
        note: str = "",
    ) -> None:
        super().__init__()
        self.title_text = title
        self._source = options
        self.multi = multi
        self.selected: List[str] = list(selected)
        self.pinned = list(pinned)
        self.note = note
        self.options: List[Choice] = []
        self.shown: List[Choice] = []
        self.is_loading = False
        self._populating = False
        self._worker: Optional[Worker] = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(self.title_text), id="pick-title")
            yield Input(placeholder="Type to filter…", id="pick-filter")
            if self.multi:
                yield SelectionList[str](id="pick-list")
            else:
                yield OptionList(id="pick-list")
            yield Static("", id="pick-status")
            hint = ("Space/Enter toggle · Ctrl+S apply · Ctrl+X clear all · Esc cancel" if self.multi
                    else "Enter choose · Esc cancel")
            yield Static(Text(hint + (f"\n{self.note}" if self.note else "")), id="pick-hint")

    def on_mount(self) -> None:
        self.query_one("#pick-filter", Input).focus()
        self.action_reload()

    def _status(self, text: str, error: bool = False) -> None:
        st = self.query_one("#pick-status", Static)
        st.update(Text(text))
        st.set_class(error, "error")

    def action_reload(self) -> None:
        if callable(self._source):
            if self.is_loading:
                return
            self.is_loading = True
            self._worker = self.run_worker(self._load(), exclusive=True, exit_on_error=False)
        else:
            self._set_options(list(self._source))

    async def _load(self) -> None:
        t0 = time.monotonic()
        timeout = ui_timeout(self)

        def tick() -> None:
            self._status(f"Loading… {time.monotonic() - t0:.1f}s  (Esc to cancel, gives up after {timeout:.0f}s)")

        timer = self.set_interval(0.25, tick)
        tick()
        try:
            opts = await guarded(self._source(), timeout)  # type: ignore[operator]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._status(f"{error_text(exc)}\nPress Ctrl+R to retry.", True)
            if self.pinned:
                self._set_options([], keep_status=True)
            return
        finally:
            timer.stop()
            self.is_loading = False
        self._set_options(list(opts))

    def _set_options(self, opts: List[Choice], keep_status: bool = False) -> None:
        seen = set()
        merged: List[Choice] = []
        for v, lbl in self.pinned + opts:
            if v not in seen:
                seen.add(v)
                merged.append((v, lbl))
        self.options = merged
        self._populate(self.query_one("#pick-filter", Input).value)
        if not keep_status:
            n = len(self.options)
            self._status(f"{n} option(s)." if n else "No options available.")

    def _populate(self, text: str) -> None:
        needle = text.strip().lower()
        self.shown = [(v, lbl) for v, lbl in self.options
                        if not needle or needle in lbl.lower() or needle in v.lower()]
        self._populating = True
        try:
            if self.multi:
                sl = self.query_one("#pick-list", SelectionList)
                sl.clear_options()
                sl.add_options([(Text(lbl), v, v in self.selected) for v, lbl in self.shown])
            else:
                ol = self.query_one("#pick-list", OptionList)
                ol.clear_options()
                ol.add_options([Option(Text(lbl), id=f"o{i}") for i, (_v, lbl) in enumerate(self.shown)])
                if self.shown:
                    ol.highlighted = 0
        finally:
            self._populating = False

    @on(Input.Changed, "#pick-filter")
    def _filter(self, ev: Input.Changed) -> None:
        ev.stop()
        self._populate(ev.value)

    @on(Input.Submitted, "#pick-filter")
    def _filter_submit(self, ev: Input.Submitted) -> None:
        ev.stop()
        if self.multi:
            self.query_one("#pick-list").focus()
            return
        ol = self.query_one("#pick-list", OptionList)
        idx = ol.highlighted if ol.highlighted is not None else 0
        if 0 <= idx < len(self.shown):
            self.dismiss([self.shown[idx][0]])

    @on(OptionList.OptionSelected, "#pick-list")
    def _chosen(self, ev: OptionList.OptionSelected) -> None:
        ev.stop()
        if 0 <= ev.option_index < len(self.shown):
            self.dismiss([self.shown[ev.option_index][0]])

    @on(SelectionList.SelectedChanged, "#pick-list")
    def _toggled(self, ev: SelectionList.SelectedChanged) -> None:
        ev.stop()
        if self._populating:
            return
        visible = {v for v, _ in self.shown}
        now = set(ev.selection_list.selected)
        keep = [v for v in self.selected if v not in visible or v in now]
        self.selected = keep + [v for v, _ in self.shown if v in now and v not in keep]

    def action_clear_selection(self) -> None:
        if self.multi:
            self.selected = []
            self._populate(self.query_one("#pick-filter", Input).value)

    def action_apply(self) -> None:
        if self.multi:
            order = {v: i for i, (v, _) in enumerate(self.options)}
            self.dismiss(sorted(self.selected, key=lambda v: order.get(v, len(order))))
        else:
            ol = self.query_one("#pick-list", OptionList)
            idx = ol.highlighted
            if idx is not None and 0 <= idx < len(self.shown):
                self.dismiss([self.shown[idx][0]])

    def action_cancel(self) -> None:
        if self._worker is not None and self._worker.is_running:
            self._worker.cancel()
        self.dismiss(None)


HELP_TEXT = """\
[b]Global[/b]
  Ctrl+Q        quit                       F1 / ?   this help
  Esc           cancel a running request; otherwise go back (asks before logging out)
  Tab/Shift+Tab move focus                 Enter    open / select
  F2 / F3 / F4  Sprints / Backlog / Issues (JQL) views
  Ctrl+N        create an issue

[b]Lists[/b]  ↑/↓ PgUp/PgDn move (more pages load automatically near the end)
         m  load next page     r  reload / retry after an error     Enter  open issue
         ]  [  sort by next / previous column (asc, desc, off); clicking a header works too

[b]Sprints & Backlog[/b]
  p project   b board   x show/hide closed sprints (Sprints)   s focus sprint list   g sprint goal (Sprints)
  l labels filter   a people filter (incl. Unassigned)
  f custom filters (apply / n new / e edit / d delete; also your Jira favourites)   F new custom filter
  X clear all filters      Filters combine: AND across kinds, OR within labels / people.

[b]Issues (JQL)[/b]
  Enter run query   → accept completion   Ctrl+R history   Ctrl+O saved queries   Ctrl+S save query
  Ctrl+T insert helper (project / type / status / assignee = currentUser())
  Ctrl+B order by   Ctrl+Y issue type filter   Ctrl+L focus the JQL input

[b]Issue[/b]   t transition   n comment   a assign   g go to linked/sub-task   R refresh
[b]Create[/b]  Ctrl+S create   pick buttons open filterable lists
[b]Pickers[/b] type to filter · Enter choose · Space toggle · Ctrl+S apply · Ctrl+X clear
"""


class HelpScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Vertical { width: 104; max-width: 100%; height: auto; max-height: 90%; border: thick $primary; background: $surface; padding: 1 2; }
    """
    BINDINGS = [Binding("escape,q,f1,question_mark", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text.from_markup(HELP_TEXT))

    def action_close(self) -> None:
        self.dismiss(None)


def kv(label: str, value: Any, style: str = "") -> Text:
    t = Text()
    t.append(f"{label}: ", style="bold")
    t.append(str(value), style=style)
    return t


def summarize(values: Sequence[str], labels: Optional[Dict[str, str]] = None, empty: str = "any",
              limit: int = 3) -> str:
    if not values:
        return empty
    shown = [(labels or {}).get(v, v) for v in values[:limit]]
    more = f" +{len(values) - limit}" if len(values) > limit else ""
    return ", ".join(shown) + more
