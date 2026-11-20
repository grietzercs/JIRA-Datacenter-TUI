"""Render Jira wiki markup (Server/DC descriptions and comments) as Rich text.

This is a pragmatic subset, aimed at readability in a terminal rather than
fidelity: headings, bold/italic/strike/underline/monospace, links, user
mentions, bullet and numbered lists, tables, ``{code}``/``{noformat}``/
``{quote}``/``{panel}`` blocks, ``bq.``, rules, images and ``{color}``.
Anything not recognised is shown verbatim.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from rich.text import Text

_BLOCK_START = re.compile(r"^\s*\{(code|noformat|quote|panel)(?::([^}]*))?\}\s*(.*)$", re.I)
_HEADING = re.compile(r"^\s*h([1-6])\.\s+(.*)$")
_LIST = re.compile(r"^\s*([*#-]+)\s+(.*)$")
_TABLE = re.compile(r"^\s*\|\|?.*\|\s*$")

# Inline tokens, tried in this order at each position.
_INLINE = re.compile(
    r"(?P<mono>\{\{(?P<mono_t>.+?)\}\})"
    r"|(?P<link>\[(?P<link_body>[^\[\]\n]+?)\])"
    r"|(?P<img>!(?P<img_t>[^!\s][^!\n]*?)!)"
    r"|(?P<color>\{color(?::[^}]*)?\})"
    r"|(?P<bold>(?<![\w*])\*(?=\S)(?P<bold_t>[^*\n]+?)(?<=\S)\*(?![\w*]))"
    r"|(?P<ital>(?<![\w_])_(?=\S)(?P<ital_t>[^_\n]+?)(?<=\S)_(?![\w_]))"
    r"|(?P<strike>(?<![\w-])-(?=\S)(?P<strike_t>[^-\n]+?)(?<=\S)-(?![\w-]))"
    r"|(?P<under>(?<![\w+])\+(?=\S)(?P<under_t>[^+\n]+?)(?<=\S)\+(?![\w+]))"
    r"|(?P<br>\\\\)"
)


def render_inline(line: str, base_style: str = "") -> Text:
    out = Text(style=base_style)
    pos = 0
    for m in _INLINE.finditer(line):
        if m.start() > pos:
            out.append(line[pos:m.start()])
        kind = m.lastgroup
        if m.group("mono"):
            out.append(m.group("mono_t"), style="bold cyan")
        elif m.group("link"):
            body = m.group("link_body")
            if body.startswith("~"):
                out.append("@" + body[1:], style="bold magenta")
            elif "|" in body:
                label, _, target = body.partition("|")
                out.append(label.strip(), style="underline")
                out.append(f" <{target.strip()}>", style="dim")
            else:
                out.append(body, style="underline")
        elif m.group("img"):
            name = m.group("img_t").split("|")[0]
            out.append(f"[image: {name}]", style="dim italic")
        elif m.group("color"):
            pass  # colour spans are dropped; terminals have their own palette
        elif m.group("bold"):
            out.append(m.group("bold_t"), style="bold")
        elif m.group("ital"):
            out.append(m.group("ital_t"), style="italic")
        elif m.group("strike"):
            out.append(m.group("strike_t"), style="strike")
        elif m.group("under"):
            out.append(m.group("under_t"), style="underline")
        elif m.group("br"):
            out.append("\n")
        else:  # pragma: no cover - every alternative is handled above
            out.append(m.group(0) if kind else "")
        pos = m.end()
    if pos < len(line):
        out.append(line[pos:])
    return out


def _table_row(line: str) -> Tuple[bool, List[str]]:
    s = line.strip()
    header = s.startswith("||")
    cells = [c.strip() for c in re.split(r"\|\|?", s.strip("|"))]
    return header, cells


def render(markup: Optional[str]) -> Text:
    """Render a whole wiki-markup document."""
    out = Text()
    if not markup or not markup.strip():
        out.append("(no description)", style="dim")
        return out
    lines = markup.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    numbering: List[int] = []
    while i < len(lines):
        line = lines[i]
        m = _BLOCK_START.match(line)
        if m:
            kind = m.group(1).lower()
            opts = m.group(2) or ""
            rest = m.group(3)
            closing = re.compile(r"\{" + kind + r"\}", re.I)
            body: List[str] = []
            # A single-line block: {code}x = 1{code}
            if closing.search(rest):
                body.append(closing.split(rest, 1)[0])
                i += 1
            else:
                if rest.strip():
                    body.append(rest)
                i += 1
                while i < len(lines) and not closing.search(lines[i]):
                    body.append(lines[i])
                    i += 1
                if i < len(lines):
                    before = closing.split(lines[i], 1)[0]
                    if before.strip():
                        body.append(before)
                    i += 1
            _render_block(out, kind, opts, body)
            numbering = []
            continue
        i += 1
        hm = _HEADING.match(line)
        if hm:
            level = int(hm.group(1))
            style = "bold underline" if level <= 2 else "bold"
            out.append_text(render_inline(hm.group(2), style))
            out.append("\n")
            numbering = []
            continue
        if line.strip() == "----":
            out.append("─" * 40 + "\n", style="dim")
            continue
        if line.lstrip().startswith("bq. "):
            out.append("│ ", style="dim")
            out.append_text(render_inline(line.lstrip()[4:], "italic"))
            out.append("\n")
            continue
        lm = _LIST.match(line)
        if lm and not (lm.group(1) == "-" and lm.group(2).endswith("-")):
            marks, body_text = lm.group(1), lm.group(2)
            depth = len(marks)
            indent = "  " * (depth - 1)
            if marks.endswith("#"):
                numbering = (numbering + [0] * depth)[:depth]
                numbering[depth - 1] += 1
                bullet = f"{numbering[depth - 1]}. "
            else:
                numbering = numbering[: depth - 1]
                bullet = "• "
            out.append(indent + bullet, style="cyan")
            out.append_text(render_inline(body_text))
            out.append("\n")
            continue
        numbering = []
        if _TABLE.match(line):
            header, cells = _table_row(line)
            for j, cell in enumerate(cells):
                if j:
                    out.append(" │ ", style="dim")
                out.append_text(render_inline(cell, "bold" if header else ""))
            out.append("\n")
            continue
        out.append_text(render_inline(line))
        out.append("\n")
    out.rstrip()
    return out


def _render_block(out: Text, kind: str, opts: str, body: List[str]) -> None:
    if kind in ("code", "noformat"):
        lang = opts.split("|")[0].strip() if kind == "code" and "=" not in opts.split("|")[0] else ""
        if lang:
            out.append(f"[{lang}]\n", style="dim")
        for b in body:
            out.append("  " + b + "\n", style="green")
    elif kind == "quote":
        for b in body:
            out.append("│ ", style="dim")
            out.append_text(render_inline(b, "italic"))
            out.append("\n")
    else:  # panel
        title = ""
        for opt in opts.split("|"):
            k, _, v = opt.partition("=")
            if k.strip().lower() == "title":
                title = v.strip()
        if title:
            out.append(title + "\n", style="bold")
        for b in body:
            out.append("┃ ", style="blue")
            out.append_text(render_inline(b))
            out.append("\n")
