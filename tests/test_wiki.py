from jiratui import wiki
from jiratui.testing.fake_server import DESCRIPTION_MARKUP


def spans(text, needle):
    """Styles applied to the first occurrence of ``needle``."""
    start = text.plain.index(needle)
    return {str(s.style) for s in text.spans if s.start <= start < s.end}


def test_inline_markup():
    t = wiki.render_inline("a *bold* and _it_ and -gone- and +u+ and {{mono}} [label|https://x.test] and [~bob]")
    assert t.plain == "a bold and it and gone and u and mono label <https://x.test> and @bob"
    assert "bold" in spans(t, "bold")
    assert "italic" in spans(t, "it ")
    assert "strike" in spans(t, "gone")
    assert "bold cyan" in spans(t, "mono")
    assert "bold magenta" in spans(t, "@bob")


def test_hyphens_and_stars_in_words_are_not_markup():
    t = wiki.render_inline("well-known re-run 2*3*4 snake_case_name a - b")
    assert t.plain == "well-known re-run 2*3*4 snake_case_name a - b"


def test_document_rendering():
    t = wiki.render(DESCRIPTION_MARKUP)
    plain = t.plain
    assert "h2." not in plain and "Problem" in plain
    assert "1. Open the login page" in plain and "  1. (with an expired cookie)" in plain and "3. Press Login" in plain
    assert "• Affects /auth/session" in plain
    assert "def login(user):" in plain and "{code" not in plain and "[python]" in plain
    assert "Env │ Result" in plain and "staging │ fails" in plain
    assert "│ Customer quote: it used to work." in plain


def test_blocks_and_misc():
    t = wiki.render("{noformat}raw *not bold*{noformat}\n{quote}\nquoted\n{quote}\n----\n"
                    "{panel:title=Note}\ninside\n{panel}\n!screen.png|thumbnail!\n{color:red}red{color}")
    plain = t.plain
    assert "raw *not bold*" in plain  # noformat is verbatim
    assert "│ quoted" in plain and "─" * 10 in plain
    assert "Note" in plain and "┃ inside" in plain
    assert "[image: screen.png]" in plain and "red" in plain and "{color" not in plain


def test_empty_description():
    assert wiki.render(None).plain == "(no description)"
    assert wiki.render("   ").plain == "(no description)"
