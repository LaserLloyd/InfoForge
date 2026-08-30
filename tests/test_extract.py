"""HTML extraction and keyword-aware truncation."""

from __future__ import annotations

from infoforge.extract import smart_truncate, strip_html


def test_strip_html_collapses_and_breaks_blocks():
    html = "<html><body><p>First para.</p><p>Second   para.</p></body></html>"
    assert strip_html(html) == "First para. Second para."


def test_strip_html_drops_script_and_style():
    html = "<div>Keep<script>var x = 1;</script><style>p{}</style>this</div>"
    assert "var x" not in strip_html(html)
    assert "p{}" not in strip_html(html)
    assert strip_html(html) == "Keep this"


def test_strip_html_decodes_entities():
    assert strip_html("<p>Ampersand &amp; dash &#8212; done</p>") == \
        "Ampersand & dash — done"


def test_strip_html_passes_through_plain_text():
    assert strip_html("no markup here") == "no markup here"


def test_strip_html_handles_empty():
    assert strip_html("") == ""


def test_short_text_is_untouched():
    assert smart_truncate("short", 100, "anything") == "short"


def test_head_truncation_when_keyword_is_early():
    text = "Jupiter is the largest planet. " + ("filler " * 500)
    out = smart_truncate(text, 200, "largest planet Jupiter")
    assert out.startswith("Jupiter is the largest planet.")
    assert out.endswith("…")
    assert "[…]" not in out  # no window needed, the answer is in the prefix


def test_window_keeps_a_late_answer_passage():
    # The answering sentence lives well past a plain head-truncation cut.
    text = ("Solar System overview. " + ("padding text. " * 400)
            + "Jupiter is the largest planet in the Solar System. "
            + ("more padding. " * 400))
    out = smart_truncate(text, 800, "largest planet Solar System")
    assert "Jupiter is the largest planet" in out
    assert out.startswith("Solar System overview.")  # lede survives
    assert "[…]" in out
    assert len(out) <= 800 + 10  # head + marker + window, no more


def test_rare_phrase_beats_common_one():
    # "Solar System" appears everywhere; "largest planet" appears once, so the
    # window must lock onto the rare phrase.
    text = ("Solar System. " * 300) + "the largest planet is Jupiter. " \
        + ("Solar System. " * 300)
    out = smart_truncate(text, 400, "largest planet Solar System")
    assert "largest planet is Jupiter" in out


def test_no_matching_keyword_falls_back_to_head():
    text = "abcdefghij" * 100
    out = smart_truncate(text, 50, "zzzz yyyy")
    assert out.startswith("abcdefghij")
    assert out.endswith("…")


def test_zero_budget_returns_empty():
    assert smart_truncate("something", 0, "something") == ""
