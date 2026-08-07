"""Turning fetched HTML into text a reader can use.

The bar is low on purpose -- drop markup, keep everything readable, never
raise -- because the step that reads this decides what matters. What these
pin is that nothing readable is silently lost and nothing unreadable leaks in.
"""

from __future__ import annotations

from agent_workbench.adapters.research.page_text import page_text


def test_markup_goes_and_prose_stays() -> None:
    html = "<html><body><h1>丹东天气</h1><p>今天 <b>晴</b> 23°/36°</p></body></html>"

    assert page_text(html, limit=500) == "丹东天气\n今天 晴 23°/36°"


def test_scripts_and_styles_are_not_prose() -> None:
    html = (
        "<body><style>.a{color:red}</style>"
        "<script>var t = '假的温度 99°';</script>"
        "<p>真的温度 23°</p></body>"
    )

    assert page_text(html, limit=500) == "真的温度 23°"


def test_table_cells_do_not_run_together() -> None:
    """A weather page is a table; `晴23°` would be one unreadable token."""

    html = "<table><tr><td>今天</td><td>晴</td><td>23°/36°</td></tr></table>"

    assert page_text(html, limit=500).split("\n") == ["今天", "晴", "23°/36°"]


def test_entities_are_decoded() -> None:
    assert page_text("<p>23&#176;C &amp; 56%</p>", limit=500) == "23\u00b0C & 56%"


def test_a_stray_closing_tag_does_not_silence_the_rest_of_the_page() -> None:
    """Real pages ship unbalanced tags; a negative counter would eat the body."""

    html = "</script><p>今天 晴</p>"

    assert "今天 晴" in page_text(html, limit=500)


def test_output_is_cut_to_the_limit() -> None:
    assert len(page_text("<p>" + "政" * 900 + "</p>", limit=100)) == 100


def test_markup_too_broken_to_parse_yields_what_was_recovered() -> None:
    """A page is data. Whatever came out before the break beats an exception."""

    assert page_text("<p>可读的<<<<>>>", limit=500).startswith("可读的")


def test_chinese_spacing_characters_collapse_like_spaces() -> None:
    """No-break and ideographic spaces are invisible in source and common here.

    Written as `\\u00a0`/`\\u3000` escapes in the pattern for exactly that
    reason, which is easy to get wrong without noticing.
    """

    html = "<p>23\u00a0\u00b0C\u3000\u6e7f\u5ea6</p>"

    assert page_text(html, limit=100) == "23 \u00b0C \u6e7f\u5ea6"


def test_an_empty_document_is_empty_text() -> None:
    assert page_text("", limit=500) == ""
