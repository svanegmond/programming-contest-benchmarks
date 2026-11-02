"""Tests for cco_eval.html_parsing."""

from cco_eval.html_parsing import cleanup_statement, html_to_text


def test_html_to_text_basic():
    html = "<p>Hello <b>world</b></p>"
    text = html_to_text(html)
    assert "Hello" in text
    assert "world" in text


def test_html_to_text_strips_script():
    html = "<p>visible</p><script>hidden();</script><p>also visible</p>"
    text = html_to_text(html)
    assert "visible" in text
    assert "also visible" in text
    assert "hidden" not in text


def test_html_to_text_h2_markers():
    html = "<h2>My Heading</h2><p>Some content</p>"
    text = html_to_text(html)
    assert "[[H2]]" in text
    assert "[[/H2]]" in text
    assert "My Heading" in text


def test_cleanup_statement_removes_markers():
    text = "[[H2]]Day 1, Problem 1: Test[[/H2]]\nSome statement."
    cleaned = cleanup_statement(text)
    assert "[[H2]]" not in cleaned
    assert "[[/H2]]" not in cleaned
    assert "Day 1, Problem 1: Test" in cleaned
