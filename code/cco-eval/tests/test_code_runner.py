"""Tests for cco_eval.code_runner."""

from cco_eval.code_runner import extract_code, output_tokens


def test_extract_code_python_fence():
    response = "Here is the solution:\n```python\nprint('hello')\n```\nDone."
    code = extract_code(response)
    assert code.strip() == "print('hello')"


def test_extract_code_generic_fence():
    response = "```\nprint('world')\n```"
    code = extract_code(response)
    assert code.strip() == "print('world')"


def test_extract_code_no_fence():
    response = "print('fallback')"
    code = extract_code(response)
    assert code.strip() == "print('fallback')"


def test_output_tokens_normalizes_newlines():
    data = b"hello\r\nworld\r\n"
    assert output_tokens(data) == [b"hello", b"world"]
