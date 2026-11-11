from pathlib import Path

import pytest

from cco_eval.waterloo import Lane
from runners.single_shot import extract_solution


def test_extracts_the_final_fenced_program_not_the_first():
    text = """Rationale.

```python
print("scratch")
```

Final:

```cpp
int main() {}
```
"""
    lang, code = extract_solution(text)
    assert lang.value == "cpp"
    assert code == "int main() {}\n"


def test_cpp_lane_rejects_a_python_only_response():
    with pytest.raises(ValueError, match="fenced"):
        extract_solution("```python\nprint(1)\n```", Lane.CPP)


def test_python_lane_keeps_the_last_python_fence():
    lang, code = extract_solution("```python\nprint(1)\n```\n```python\nprint(2)\n```", Lane.PYTHON)
    assert lang.value == "python"
    assert code == "print(2)\n"


def test_recovers_saved_truncated_shapes():
    fixtures = Path(__file__).parent / "fixtures/waterloo"
    bare = (fixtures / "gemini-2025-truncated-response.txt").read_text(encoding="utf-8")
    lang, code = extract_solution(bare)
    assert lang.value == "cpp"
    assert code.startswith("#include <iostream>")
    unclosed = (fixtures / "gemini-2024-unclosed-fence-response.txt").read_text(encoding="utf-8")
    lang, code = extract_solution(unclosed)
    assert lang.value == "cpp"
    assert "int main()" in code
