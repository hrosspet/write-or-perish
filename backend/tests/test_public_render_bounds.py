"""The server-rendered public pages render markdown within a time and
size bound: a body that would take long to parse (long runs of
unmatched brackets or emphasis markers) is shown as plain paragraphs,
and a page renders at most PAGE_RENDER_CHARS of markdown.

Fixtures from test_public_pages (in-memory sqlite, no page cache).
"""
import time

import mistune
import pytest

from backend.tests.test_public_pages import (  # noqa: F401 (fixtures)
    ARTICLE, _mk_node, _publish, app,
)

# Each of these took from seconds to many minutes to render at this size
# before the bound (100,000 characters is the node size cap). "[a](" and
# "[a](b (" (a destination, then a title left open) take minutes inside
# mistune 3.3.4's link index, which Python 3.9 installs.
SLOW_RUNS = ["[", "![", "*a ", "~~a ", "[a](", "[a](b (", "[a][", "|"]

# Generous: the bound is 0.5 s of CPU per body. Measured as this thread's
# CPU time, as the bound is, so a busy machine does not fail the tests.
CPU_LIMIT = 5.0


def _run(unit, n):
    return (unit * (n // len(unit) + 1))[:n]


@pytest.mark.parametrize("unit", SLOW_RUNS)
def test_long_delimiter_run_renders_in_bounded_time(app, unit):
    from backend.utils.public_html import render_markdown
    text = _run(unit, 100_000)
    started = time.thread_time()
    html = render_markdown(text)
    assert time.thread_time() - started < CPU_LIMIT
    # The text is still there, escaped.
    assert unit.strip()[0] in html


# Openers that are not closed, inside a paragraph (at the start of a line
# some of them begin block HTML instead): each is read up to its closing
# marker within the inline pattern's one search.
UNCLOSED_INLINE = ["a<?", "a<!A", "a<![CDATA[", "a<?b<!A", "a<!A<?",
                   "a<!Ab<!", "a<!--", "a<a b='", 'a<a b="', "a<x@y",
                   "a<a:", "a~~", "a`", "a**", "a__"]


@pytest.mark.parametrize("unit", UNCLOSED_INLINE)
def test_unclosed_inline_openers_in_a_paragraph_render_in_bounded_time(
        app, unit):
    from backend.utils.public_html import render_markdown
    text = _run(unit, 100_000)
    started = time.thread_time()
    html = render_markdown(text)
    assert time.thread_time() - started < CPU_LIMIT
    assert html.startswith("<p>a")


def test_closed_inline_html_spans_render_as_before(app):
    from backend.utils.public_html import render_markdown
    reference = mistune.create_markdown(
        escape=True, plugins=["table", "strikethrough", "url", "task_lists"])
    for text in ("a <?php echo 1 ?> b *c*", "x <!DOCTYPE html> y *z*",
                 "c <![CDATA[ x ]]> d *e*", "f <!-- note --> g *h*"):
        assert render_markdown(text) == reference(text)


def test_ordinary_markdown_renders_as_before(app):
    from backend.utils.public_html import render_markdown
    reference = mistune.create_markdown(
        escape=True, plugins=["table", "strikethrough", "url", "task_lists"])
    sample = (
        "Some *em* and **strong** and ~~gone~~ [a link](https://x.example/a)"
        " ![pic](/media/nodes/1/2/a.png)\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "c | d\n--|--\n3 | 4\n\n"
        "- [ ] open\n- [x] done\n\nhttps://auto.example/link\n\n"
        "> quoted\n\n```\ncode [ * |\n```\n\n## Heading\n\n<b>raw</b>\n")
    assert render_markdown(sample) == reference(sample)
    assert render_markdown(sample).count("<table>") == 2


def test_spent_page_budget_shows_plain_paragraphs(app):
    from backend.utils.public_html import RenderBudget, render_markdown
    budget = RenderBudget(seconds=0)
    html = render_markdown("first *line*\nsecond <b>x</b>\n\nnext", budget)
    assert html == ("<p>first *line*<br />\nsecond &lt;b&gt;x&lt;/b&gt;</p>\n"
                    "<p>next</p>\n")


def test_render_limit_counts_cpu_time_not_waiting(app, monkeypatch):
    """Time the thread spends off the CPU (here: asleep inside the parse)
    does not count against a body's render time."""
    from backend.utils import public_html
    parse_method = public_html._TimedInlineParser.parse_method

    def slow_off_cpu(self, m, state):
        time.sleep(0.2)
        return parse_method(self, m, state)
    monkeypatch.setattr(public_html._TimedInlineParser, "parse_method",
                        slow_off_cpu)
    started = time.monotonic()
    html = public_html.render_markdown("a *b* c *d* e *f* g")
    assert time.monotonic() - started > public_html.BODY_RENDER_SECONDS
    assert html == "<p>a <em>b</em> c <em>d</em> e <em>f</em> g</p>\n"


def test_fallback_logs_the_node_and_length_only(app, caplog, monkeypatch):
    import itertools
    import logging
    from backend.utils import public_html
    from backend.utils.public_html import RenderBudget, render_markdown
    text = "a private sentence " + "[" * 1_000
    # A render clock that moves a second at each reading, so the body's
    # render time is spent at the parser's first check whatever the
    # mistune release (newer ones parse bracket runs in linear time) and
    # however fast the machine.
    seconds = itertools.count()
    with monkeypatch.context() as patched, caplog.at_level(
            logging.WARNING, logger="backend.utils.public_html"):
        patched.setattr(public_html, "_render_clock",
                        lambda: float(next(seconds)))
        render_markdown(text, node_id=4242)
    lines = [r.getMessage() for r in caplog.records]
    assert lines == [f"Public page: node 4242 ({len(text)} characters) "
                     "shown as plain paragraphs (render time)"]

    caplog.clear()
    budget = RenderBudget(seconds=0)
    with caplog.at_level(logging.WARNING, logger="backend.utils.public_html"):
        for node_id in (1, 2, 3):
            render_markdown("a private sentence", budget, node_id=node_id)
    # One line per page once its render time is spent, not one per body.
    assert [r.getMessage() for r in caplog.records] == [
        "Public page: node 1 (18 characters) shown as plain paragraphs "
        "(the page's render time was spent)"]
    assert "private" not in caplog.text


def test_plain_excerpt_of_bracket_runs_is_quick(app):
    from backend.utils.public_html import plain_excerpt
    for unit in ("[", "![", "<", "[a](", "![a]("):
        text = _run(unit, 100_000)
        started = time.thread_time()
        plain_excerpt(text)
        assert time.thread_time() - started < CPU_LIMIT
    assert plain_excerpt(
        "See [Foo](https://example.org/a) and ![x](/media/a.png) "
        "<b>bold</b> `code`") == "See Foo and bold code"


@pytest.mark.parametrize("path", ["node", "article", "feed"])
def test_public_page_with_a_long_bracket_run_answers_quickly(app, path):
    root = _publish("author", "# Brackets\n\n" + "[" * 20_000, "brackets")
    url = {"node": f"/node/{root.id}", "article": "/@author/brackets",
           "feed": "/@author/feed.xml"}[path]
    started = time.thread_time()
    r = app.test_client().get(url)
    assert r.status_code == 200
    assert time.thread_time() - started < CPU_LIMIT
    assert "[[[[[[[[" in r.get_data(as_text=True)


def test_reply_under_a_public_piece_is_bounded_too(app):
    root = _publish("author", ARTICLE, "on-lore")
    _mk_node("visitor", "![" * 10_000, parent=root)
    _mk_node("visitor", "a later reply", parent=root)
    started = time.thread_time()
    html = app.test_client().get("/@author/on-lore").get_data(as_text=True)
    assert time.thread_time() - started < CPU_LIMIT
    assert "Everything you say is kept." in html
    assert "a later reply" in html


def test_thread_beyond_the_page_size_leaves_later_replies_to_the_app(
        app, monkeypatch):
    from backend.utils import public_html
    monkeypatch.setattr(public_html, "PAGE_RENDER_CHARS", 300)
    root = _publish("author", ARTICLE, "on-lore")
    _mk_node("visitor", "first reply " + "x" * 100, parent=root)
    _mk_node("visitor", "second reply " + "y" * 300, parent=root)
    _mk_node("visitor", "third reply", parent=root)
    html = app.test_client().get("/@author/on-lore").get_data(as_text=True)
    assert "Everything you say is kept." in html
    assert "first reply" in html
    assert "second reply" not in html
    assert "third reply" not in html
