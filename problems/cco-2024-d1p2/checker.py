"""Validator for CCO 2024 Day 1 Problem 2 (Pizza Party).

Mirrors the official testlib checker shipped in the ZIP: K must equal the
jury's minimum (-1 when impossible), and the c and d lines must simulate to a
valid pizza arrangement in which every person finds their flavour on top.
"""


def _int(token: str) -> int | None:
    if len(token) > 12 or not token.isascii():
        return None
    body = token[1:] if token[:1] == "-" else token
    return int(token) if body.isdigit() else None


def _result(ok: bool, message: str) -> dict:
    return {"ok": ok, "score": 1.0 if ok else 0.0, "message": message}


def check(input_text: str, expected_text: str, output_text: str) -> dict:
    fields = input_text.split()
    n = int(fields[0])
    pizzas = list(map(int, fields[1 : 1 + n]))
    wanted = list(map(int, fields[1 + n : 1 + 2 * n]))
    if len(wanted) != n:
        raise ValueError("input does not have N, a and b lines")

    jury = int(expected_text.split()[0])
    tokens = output_text.split()
    if not tokens:
        return _result(False, "empty output")
    stacks_used = _int(tokens[0])
    if stacks_used is None or not (stacks_used == -1 or 1 <= stacks_used <= n):
        return _result(False, f"K must be -1 or an integer in [1, N]: {tokens[0][:20]!r}")
    if stacks_used != jury:
        return _result(False, f"K={stacks_used} but the minimum is {jury}")
    if stacks_used == -1:
        if len(tokens) != 1:
            return _result(False, "extra tokens after -1")
        return _result(True, "impossible")

    if len(tokens) != 1 + 2 * n:
        return _result(False, f"expected {1 + 2 * n} tokens, got {len(tokens)}")
    placement = [_int(token) for token in tokens[1 : 1 + n]]
    pickup = [_int(token) for token in tokens[1 + n :]]
    if any(value is None or not 1 <= value <= stacks_used for value in placement + pickup):
        return _result(False, "c_i and d_i must be integers in [1, K]")

    stacks: list[list[int]] = [[] for _ in range(stacks_used)]
    for flavour, stack in zip(pizzas, placement):
        stacks[stack - 1].append(flavour)
    for person, (flavour, stack) in enumerate(zip(wanted, pickup), start=1):
        pile = stacks[stack - 1]
        if not pile:
            return _result(False, f"person {person}: stack {stack} is empty")
        if pile[-1] != flavour:
            return _result(False, f"person {person}: stack {stack} has flavour {pile[-1]} on top, wanted {flavour}")
        pile.pop()
    return _result(True, f"valid with {stacks_used} stacks")
