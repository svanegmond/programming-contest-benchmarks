"""Scorer for CCO 2020 Day 2 Problem 1 (Travelling Salesperson).

Reimplements the statement's scoring. K_i, the optimal route length from
building i, is taken from the official output. Every official M_i equals N,
which is optimal because a route visits N distinct buildings. Each plan scores
0 if M_i > 2K_i, 25 if M_i = K_i, else floor(8 + 8(2K_i - M_i)/(K_i - 1)).
The test score is the minimum over plans, returned as a fraction of 25. An
invalid plan scores 0 for the whole test.
"""

FULL_SCORE = 25


def _int(token: str) -> int | None:
    if len(token) > 12 or not token.isascii():
        return None
    return int(token) if token.isdigit() else None


def _result(score: float, message: str) -> dict:
    return {"ok": score >= 1.0, "score": score, "message": message}


def plan_score(optimal: int, length: int) -> int:
    if length <= optimal:
        return FULL_SCORE
    if length > 2 * optimal:
        return 0
    return 8 + (8 * (2 * optimal - length)) // (optimal - 1)


def check(input_text: str, expected_text: str, output_text: str) -> dict:
    lines = input_text.split()
    n = int(lines[0])
    rows = lines[1:]
    if len(rows) != n - 1:
        raise ValueError("input does not have N-1 colour rows")

    def colour(a: int, b: int) -> str:
        return rows[a - 2][b - 1] if a > b else rows[b - 2][a - 1]

    optimal: list[int] = []
    expected = expected_text.split()
    cursor = 0
    for _ in range(n):
        length = int(expected[cursor])
        optimal.append(length)
        cursor += 1 + length

    tokens = output_text.split()
    cursor = 0
    worst = FULL_SCORE
    for start in range(1, n + 1):
        length = _int(tokens[cursor]) if cursor < len(tokens) else None
        if length is None or length < 2:
            return _result(0.0, f"plan {start}: missing or invalid length")
        if length > 2 * optimal[start - 1]:
            return _result(0.0, f"plan {start}: length {length} exceeds twice the optimum {optimal[start - 1]}")
        route = tokens[cursor + 1 : cursor + 1 + length]
        cursor += 1 + length
        if len(route) != length:
            return _result(0.0, f"plan {start}: fewer than {length} buildings")
        stops = [_int(token) for token in route]
        if stops[0] != start:
            return _result(0.0, f"plan {start}: route does not begin at building {start}")
        seen = bytearray(n + 1)
        previous = start
        seen[start] = 1
        last_colour = None
        tickets = 0
        for stop in stops[1:]:
            if stop is None or not 1 <= stop <= n or stop == previous:
                return _result(0.0, f"plan {start}: invalid step to {stop}")
            seen[stop] = 1
            edge = colour(previous, stop)
            if last_colour is not None and edge != last_colour:
                tickets += 1
                if tickets > 1:
                    return _result(0.0, f"plan {start}: uses more than one ticket")
            last_colour = edge
            previous = stop
        if sum(seen) != n:
            return _result(0.0, f"plan {start}: does not visit every building")
        worst = min(worst, plan_score(optimal[start - 1], length))
    if cursor != len(tokens):
        return _result(0.0, "extra tokens after the last plan")
    return _result(worst / FULL_SCORE, f"minimum plan score {worst}/{FULL_SCORE}")
