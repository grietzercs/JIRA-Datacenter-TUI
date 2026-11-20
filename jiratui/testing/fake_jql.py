"""A small JQL parser/evaluator for the fake Jira server.

It supports what jira-tui generates and what people commonly type:

* ``AND`` / ``OR`` / ``NOT`` and parentheses;
* operators ``= != ~ !~ > >= < <= in, not in, is, is not``;
* quoted (``"..."`` / ``'...'`` with backslash escapes) and bare values,
  lists, ``EMPTY``/``NULL`` and functions (``currentUser()``,
  ``openSprints()``, ``closedSprints()``, ``futureSprints()``, ``now()``,
  ``startOfDay()``);
* field names bare, quoted (``"Story Points"``) or ``cf[10002]``;
* ``ORDER BY f [ASC|DESC], ...``.

Errors are reported the way Jira does (``Error in the JQL Query: ...``,
``Field 'x' does not exist ...``, ``The value 'x' does not exist for the field
'y'.``) so the UI's error display can be exercised realistically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence, Tuple

Pred = Callable[[Any], bool]


class JqlSyntaxError(Exception):
    """Carries Jira-style ``errorMessages``."""

    def __init__(self, *messages: str) -> None:
        super().__init__("; ".join(messages))
        self.messages = list(messages)


@dataclass
class Tok:
    kind: str  # STR | WORD | OP | LP | RP | COMMA | EOF
    value: str
    pos: int


_OPS = ("!=", "!~", ">=", "<=", "=", "~", ">", "<")


def tokenize(text: str) -> List[Tok]:
    toks: List[Tok] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in ("'", '"'):
            q = ch
            j = i + 1
            buf = []
            while j < n and text[j] != q:
                if text[j] == "\\" and j + 1 < n:
                    nxt = text[j + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                raise JqlSyntaxError(f"Error in the JQL Query: The quoted string starting at character {i + 1} "
                                     "has not been completed. (line 1, character %d)" % (i + 1))
            toks.append(Tok("STR", "".join(buf), i))
            i = j + 1
            continue
        if ch == "(":
            toks.append(Tok("LP", ch, i))
            i += 1
            continue
        if ch == ")":
            toks.append(Tok("RP", ch, i))
            i += 1
            continue
        if ch == ",":
            toks.append(Tok("COMMA", ch, i))
            i += 1
            continue
        op = next((o for o in _OPS if text.startswith(o, i)), None)
        if op:
            toks.append(Tok("OP", op, i))
            i += len(op)
            continue
        m = re.match(r"[^\s=!~<>(),\"']+", text[i:])
        if not m:  # a stray '!' etc.
            raise JqlSyntaxError(f"Error in the JQL Query: The character '{ch}' is a reserved JQL character. "
                                 f"(line 1, character {i + 1})")
        word = m.group(0)
        # cf[10002] is one word even though it contains brackets.
        toks.append(Tok("WORD", word, i))
        i += len(word)
    toks.append(Tok("EOF", "", n))
    return toks


@dataclass
class Value:
    kind: str  # str | empty | func | list
    text: str = ""
    items: Tuple["Value", ...] = ()


@dataclass
class Clause:
    field: str
    op: str  # = != ~ !~ > >= < <= in "not in" is "is not"
    value: Value


class Context:
    """Implemented by the fake server: field access and value validation."""

    def field_kind(self, field: str) -> Optional[str]:  # pragma: no cover - interface
        """'text' | 'list' | 'number' | 'date' | None (unknown field)."""
        raise NotImplementedError

    def values(self, issue: Any, field: str) -> List[str]:  # pragma: no cover
        raise NotImplementedError

    def function(self, name: str, field: str) -> List[str]:  # pragma: no cover
        raise NotImplementedError

    def validate_value(self, field: str, value: str) -> Optional[str]:  # pragma: no cover
        return None

    def sort_key(self, issue: Any, field: str) -> Any:  # pragma: no cover
        raise NotImplementedError


class Parser:
    def __init__(self, text: str, ctx: Context) -> None:
        self.text = text
        self.toks = tokenize(text)
        self.i = 0
        self.ctx = ctx
        self.errors: List[str] = []

    # -------------------------------------------------------------- helpers
    @property
    def tok(self) -> Tok:
        return self.toks[self.i]

    def _word(self, *words: str) -> bool:
        return self.tok.kind == "WORD" and self.tok.value.lower() in words

    def _next(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def _fail(self, expected: str) -> JqlSyntaxError:
        t = self.tok
        got = "end of query" if t.kind == "EOF" else repr(t.value).replace('"', "'")
        return JqlSyntaxError(
            f"Error in the JQL Query: Expecting {expected} but got {got}. "
            f"The invalid query was '{self.text}' (line 1, character {t.pos + 1})"
        )

    # -------------------------------------------------------------- grammar
    def parse(self) -> Tuple[Pred, List[Tuple[str, bool]]]:
        if self.tok.kind == "EOF" or self._word("order"):
            pred: Pred = lambda issue: True  # noqa: E731
        else:
            pred = self._or()
        order: List[Tuple[str, bool]] = []
        if self._word("order"):
            self._next()
            if not self._word("by"):
                raise self._fail("'by'")
            self._next()
            while True:
                if self.tok.kind not in ("WORD", "STR"):
                    raise self._fail("a field name")
                f = self._next().value
                desc = False
                if self._word("asc", "desc"):
                    desc = self._next().value.lower() == "desc"
                if self.ctx.field_kind(_norm(f)) is None:
                    self.errors.append(f"Not able to sort using field '{f}'.")
                order.append((_norm(f), desc))
                if self.tok.kind == "COMMA":
                    self._next()
                    continue
                break
        if self.tok.kind != "EOF":
            raise self._fail("either 'OR' or 'AND'" if not order else "',' or end of query")
        if self.errors:
            raise JqlSyntaxError(*self.errors)
        return pred, order

    def _or(self) -> Pred:
        parts = [self._and()]
        while self._word("or"):
            self._next()
            parts.append(self._and())
        if len(parts) == 1:
            return parts[0]
        return lambda issue: any(p(issue) for p in parts)

    def _and(self) -> Pred:
        parts = [self._not()]
        while self._word("and"):
            self._next()
            parts.append(self._not())
        if len(parts) == 1:
            return parts[0]
        return lambda issue: all(p(issue) for p in parts)

    def _not(self) -> Pred:
        if self._word("not"):
            self._next()
            inner = self._not()
            return lambda issue: not inner(issue)
        if self.tok.kind == "LP":
            self._next()
            inner = self._or()
            if self.tok.kind != "RP":
                raise self._fail("')'")
            self._next()
            return inner
        return self._clause()

    def _clause(self) -> Pred:
        if self.tok.kind not in ("WORD", "STR"):
            raise self._fail("a field name")
        raw_field = self._next().value
        field = _norm(raw_field)
        op = self._operator()
        value = self._value(op)
        kind = self.ctx.field_kind(field)
        if kind is None:
            self.errors.append(f"Field '{raw_field}' does not exist or you do not have permission to view it.")
            return lambda issue: False
        if op in ("~", "!~") and kind != "text":
            self.errors.append(f"The operator '{op}' is not supported by the '{raw_field}' field.")
        if op in ("=", "!=", "in", "not in") and kind == "text":
            self.errors.append(f"The operator '{op}' is not supported by the '{raw_field}' field.")
        for v in _flatten(value):
            if v.kind == "str":
                problem = self.ctx.validate_value(field, v.text)
                if problem:
                    self.errors.append(problem)
        return self._predicate(Clause(field, op, value), kind)

    def _operator(self) -> str:
        t = self.tok
        if t.kind == "OP":
            self._next()
            return t.value
        if self._word("in"):
            self._next()
            return "in"
        if self._word("not"):
            self._next()
            if not self._word("in"):
                raise self._fail("'in'")
            self._next()
            return "not in"
        if self._word("is"):
            self._next()
            if self._word("not"):
                self._next()
                return "is not"
            return "is"
        raise self._fail("operator")

    def _value(self, op: str) -> Value:
        t = self.tok
        if op in ("in", "not in"):
            if t.kind != "LP":
                if t.kind == "WORD" and self.toks[self.i + 1].kind == "LP":
                    return self._single()  # in openSprints()
                raise self._fail("'('")
            self._next()
            items = [self._single()]
            while self.tok.kind == "COMMA":
                self._next()
                items.append(self._single())
            if self.tok.kind != "RP":
                raise self._fail("')'")
            self._next()
            return Value("list", items=tuple(items))
        v = self._single()
        if op in ("is", "is not") and v.kind != "empty":
            raise JqlSyntaxError(f"Error in the JQL Query: The operator '{op.upper()}' can only be used with "
                                 f"EMPTY or NULL. (line 1, character {t.pos + 1})")
        return v

    def _single(self) -> Value:
        t = self.tok
        if t.kind == "STR":
            self._next()
            return Value("str", t.value)
        if t.kind == "WORD":
            self._next()
            if t.value.lower() in ("empty", "null"):
                return Value("empty")
            if self.tok.kind == "LP":
                self._next()
                while self.tok.kind not in ("RP", "EOF"):
                    self._next()  # arguments are ignored by the fake
                if self.tok.kind != "RP":
                    raise self._fail("')'")
                self._next()
                return Value("func", t.value.lower())
            return Value("str", t.value)
        raise self._fail("a value")

    # ----------------------------------------------------------- evaluation
    def _resolve(self, field: str, value: Value) -> Optional[List[str]]:
        """Values to compare against (lower-cased); None means EMPTY."""
        if value.kind == "empty":
            return None
        if value.kind == "func":
            got = self.ctx.function(value.text, field)
            if got is None:
                self.errors.append(f"Unable to find JQL function '{value.text}()'.")
                return []
            return [g.lower() for g in got]
        if value.kind == "list":
            out: List[str] = []
            for it in value.items:
                out.extend(self._resolve(field, it) or [])
            return out
        return [value.text.lower()]

    def _predicate(self, c: Clause, kind: str) -> Pred:
        ctx = self.ctx
        targets = self._resolve(c.field, c.value)
        field, op = c.field, c.op

        def vals(issue: Any) -> List[str]:
            return [v.lower() for v in ctx.values(issue, field)]

        if op == "is":
            return lambda issue: not vals(issue)
        if op == "is not":
            return lambda issue: bool(vals(issue))
        if targets is None:  # = EMPTY / != EMPTY
            if op in ("=", "in"):
                return lambda issue: not vals(issue)
            return lambda issue: bool(vals(issue))
        tset = set(targets)
        if op in ("=", "in"):
            return lambda issue: bool(tset & set(vals(issue)))
        if op in ("!=", "not in"):
            return lambda issue: bool(vals(issue)) and not (tset & set(vals(issue)))
        if op in ("~", "!~"):
            words = [w for t in targets for w in re.findall(r"[\w'/:.-]+", t)]

            def contains(issue: Any) -> bool:
                hay = " ".join(vals(issue))
                return all(w.strip("*") in hay for w in words)

            return contains if op == "~" else (lambda issue: not contains(issue))
        # ordering comparisons
        target = targets[0] if targets else ""

        def cmp(issue: Any) -> bool:
            for v in vals(issue):
                a, b = _num(v), _num(target)
                left, right = (a, b) if a is not None and b is not None else (v, target)
                if (op == ">" and left > right) or (op == ">=" and left >= right) or \
                        (op == "<" and left < right) or (op == "<=" and left <= right):
                    return True
            return False

        return cmp


def _flatten(v: Value) -> List[Value]:
    return [x for it in v.items for x in _flatten(it)] if v.kind == "list" else [v]


def _num(s: str) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _norm(field: str) -> str:
    f = field.strip().strip('"').strip("'").lower()
    m = re.match(r"^customfield_(\d+)$", f)
    if m:
        return f"cf[{m.group(1)}]"
    return f


def run(jql: str, issues: Sequence[Any], ctx: Context) -> List[Any]:
    """Filter and order ``issues``; raises :class:`JqlSyntaxError`."""
    pred, order = Parser(jql or "", ctx).parse()
    out = [i for i in issues if pred(i)]
    for field, desc in reversed(order):
        out.sort(key=lambda i: ctx.sort_key(i, field), reverse=desc)
    return out
