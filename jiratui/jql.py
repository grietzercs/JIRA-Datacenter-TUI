"""JQL construction helpers.

Everything the UI generates goes through here so quoting is consistent:

* every *value* is emitted as a double-quoted JQL string with ``\\`` and ``"``
  escaped, so labels like ``ux/ui``, ``release:2.0`` or ``o'reilly`` and
  usernames like ``j.smith@corp`` or ``Mary Ann`` are always safe;
* *field names* are left bare when they are plain identifiers, emitted as
  ``cf[NNNNN]`` for custom field ids, and quoted otherwise (``"Story Points"``);
* fragments are combined with AND. A fragment that has a top-level ``OR`` is
  parenthesised so it cannot change the meaning of its neighbours, and any
  ``ORDER BY`` inside a fragment is dropped (only the final query orders).
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Tuple

UNASSIGNED = "__unassigned__"  # sentinel in "people" filters for `assignee is EMPTY`

_BARE_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*$")
_CUSTOM_ID = re.compile(r"^customfield_(\d+)$")
_RESERVED_FIELD_WORDS = {"and", "or", "not", "empty", "null", "order", "by", "in", "is"}


def quote_value(value: str) -> str:
    """Quote a JQL value: ``a "b"\\c`` -> ``"a \\"b\\"\\\\c"``."""
    s = str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{s}"'


def field_ref(name: str) -> str:
    """JQL reference for a field name or id."""
    name = str(name).strip()
    m = _CUSTOM_ID.match(name)
    if m:
        return f"cf[{m.group(1)}]"
    if re.match(r"^cf\[\d+\]$", name, re.I):
        return name
    if _BARE_FIELD.match(name) and name.lower() not in _RESERVED_FIELD_WORDS:
        return name
    return quote_value(name)


def value_list(values: Iterable[str]) -> str:
    return "(" + ", ".join(quote_value(v) for v in values) + ")"


def in_clause(field: str, values: Sequence[str]) -> str:
    vals = [v for v in values if str(v) != ""]
    if not vals:
        return ""
    if len(vals) == 1:
        return f"{field_ref(field)} = {quote_value(vals[0])}"
    return f"{field_ref(field)} in {value_list(vals)}"


def labels_clause(labels: Sequence[str]) -> str:
    """OR within labels: ``labels in ("a", "b")`` matches issues having any of them."""
    vals = [v for v in labels if v]
    return f"labels in {value_list(vals)}" if vals else ""


def people_clause(people: Sequence[str], field: str = "assignee") -> str:
    """OR within people; :data:`UNASSIGNED` becomes ``assignee is EMPTY``."""
    names = [p for p in people if p and p != UNASSIGNED]
    unassigned = UNASSIGNED in people
    ref = field_ref(field)
    parts = []
    if names:
        parts.append(f"{ref} in {value_list(names)}")
    if unassigned:
        parts.append(f"{ref} is EMPTY")
    if len(parts) == 2:
        return f"({parts[0]} OR {parts[1]})"
    return parts[0] if parts else ""


def text_clause(text: str) -> str:
    t = (text or "").strip()
    return f"text ~ {quote_value(t)}" if t else ""


# ------------------------------------------------------------ tokenising
def _scan(jql: str):
    """Yield ``(index, char, depth, in_quote)`` for every character of ``jql``."""
    depth = 0
    quote: Optional[str] = None
    escaped = False
    for i, ch in enumerate(jql):
        if quote:
            yield i, ch, depth, True
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            yield i, ch, depth, True
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        yield i, ch, depth, False


def _top_level_words(jql: str) -> List[Tuple[int, str]]:
    """``(start, word_lower)`` for bare words at parenthesis depth 0, outside quotes."""
    words: List[Tuple[int, str]] = []
    cur = ""
    start = 0
    for i, ch, depth, in_q in _scan(jql):
        if not in_q and depth == 0 and (ch.isalnum() or ch == "_"):
            if not cur:
                start = i
            cur += ch
        else:
            if cur:
                words.append((start, cur.lower()))
            cur = ""
    if cur:
        words.append((start, cur.lower()))
    return words


def split_order_by(jql: str) -> Tuple[str, str]:
    """Split ``... ORDER BY x DESC`` into ``("...", "x DESC")`` (quote-aware)."""
    words = _top_level_words(jql)
    for (i, w), (_, nxt) in zip(words, words[1:]):
        if w == "order" and nxt == "by":
            where = jql[:i].strip()
            order = re.sub(r"^\s*order\s+by\s*", "", jql[i:], flags=re.I).strip()
            return where, order
    return jql.strip(), ""


def strip_order_by(jql: str) -> str:
    return split_order_by(jql)[0]


def set_order_by(jql: str, field: str, direction: str = "ASC") -> str:
    """Replace the ORDER BY of ``jql`` (keeping its WHERE part)."""
    where, _ = split_order_by(jql)
    direction = "DESC" if direction.upper().startswith("D") else "ASC"
    order = f"ORDER BY {field_ref(field)} {direction}"
    return f"{where} {order}".strip()


def has_top_level_or(fragment: str) -> bool:
    return any(w == "or" for _, w in _top_level_words(fragment))


def _wrap(fragment: str) -> str:
    f = fragment.strip()
    if not f:
        return ""
    words = _top_level_words(f)
    if has_top_level_or(f) or (words and words[0][1] == "not"):
        return f"({f})"
    return f


def and_join(fragments: Iterable[str]) -> str:
    """AND non-empty fragments together (ORDER BY clauses inside are dropped)."""
    parts = [_wrap(strip_order_by(f)) for f in fragments if f and strip_order_by(f).strip()]
    return " AND ".join(p for p in parts if p)


def compose(base: str, fragments: Sequence[str] = (), order_by: str = "") -> str:
    """``base AND frag... ORDER BY ...``.

    ``order_by`` (without the keywords) overrides the base query's own ORDER BY;
    otherwise the base ordering is kept.
    """
    where, base_order = split_order_by(base or "")
    query = and_join([where, *fragments])
    order = order_by or base_order
    if order:
        query = f"{query} ORDER BY {order}".strip()
    return query


def sprint_clause(sprint_id: int) -> str:
    return f"sprint = {int(sprint_id)}"


def project_clause(project_key: str) -> str:
    return f"project = {quote_value(project_key)}"


def backlog_base(project_key: str) -> str:
    """JQL equivalent of a backlog for kanban / board-less projects."""
    return (f"{project_clause(project_key)} AND (sprint is EMPTY OR sprint in closedSprints()) "
            "AND statusCategory != Done")
