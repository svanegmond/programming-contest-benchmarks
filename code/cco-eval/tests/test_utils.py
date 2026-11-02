"""Tests for cco_eval.utils."""

from cco_eval.utils import compact_ws, int_or_none, natural_key, sha256_text, split_csv, utc_now


def test_utc_now_format():
    ts = utc_now()
    assert ts.endswith("Z")
    assert "T" in ts


def test_sha256_text_deterministic():
    assert sha256_text("hello") == sha256_text("hello")
    assert sha256_text("hello") != sha256_text("world")
    assert len(sha256_text("hello")) == 64


def test_compact_ws():
    assert compact_ws("  hello   world  ") == "hello world"
    assert compact_ws("a\n\n\tb") == "a b"
    assert compact_ws("\xa0test\xa0") == "test"


def test_int_or_none():
    assert int_or_none(42) == 42
    assert int_or_none(True) is None
    assert int_or_none(False) is None
    assert int_or_none("hello") is None
    assert int_or_none(None) is None


def test_split_csv():
    assert split_csv("a,b,c") == ["a", "b", "c"]
    assert split_csv("  a , b , c  ") == ["a", "b", "c"]
    assert split_csv("") == []
    assert split_csv(None) == []


def test_natural_key():
    items = ["p10", "p2", "p1"]
    assert sorted(items, key=natural_key) == ["p1", "p2", "p10"]
