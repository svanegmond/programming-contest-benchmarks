"""Validator for CCO 2022 Day 1 Problem 2 (Rainy Markets).

YES/NO and the minimum umbrella cost are fixed by the statement, so they are
compared with the official output. The per-market split is any assignment that
respects every capacity and the umbrella stock.
"""


def _int(token: str) -> int | None:
    if len(token) > 19 or not token.isascii():
        return None
    return int(token) if token.isdigit() else None


def _result(ok: bool, message: str) -> dict:
    return {"ok": ok, "score": 1.0 if ok else 0.0, "message": message}


def check(input_text: str, expected_text: str, output_text: str) -> dict:
    fields = input_text.split()
    n = int(fields[0])
    shelters = list(map(int, fields[1 : 1 + n]))
    people = list(map(int, fields[1 + n : 2 * n]))
    umbrellas = list(map(int, fields[2 * n : 3 * n - 1]))
    if len(umbrellas) != n - 1:
        raise ValueError("input does not have N, B, P and U lines")

    expected = expected_text.split()
    tokens = output_text.split()
    if not tokens:
        return _result(False, "empty output")
    if tokens[0] not in ("YES", "NO"):
        return _result(False, f"first token must be YES or NO: {tokens[0][:20]!r}")
    if tokens[0] != expected[0]:
        return _result(False, f"expected {expected[0]}, got {tokens[0]}")
    if tokens[0] == "NO":
        if len(tokens) != 1:
            return _result(False, "extra tokens after NO")
        return _result(True, "NO")

    if len(tokens) != 2 + 3 * (n - 1):
        return _result(False, f"expected {2 + 3 * (n - 1)} tokens, got {len(tokens)}")
    cost = _int(tokens[1])
    if cost is None or cost != int(expected[1]):
        return _result(False, f"cost {tokens[1][:20]!r} is not the minimum {expected[1]}")

    load = [0] * n
    spent = 0
    for market in range(n - 1):
        x, y, z = (_int(token) for token in tokens[2 + 3 * market : 5 + 3 * market])
        if x is None or y is None or z is None:
            return _result(False, f"market {market + 1} has a non-integer or negative count")
        if x + y + z != people[market]:
            return _result(False, f"market {market + 1} places {x + y + z} of {people[market]} people")
        if y > umbrellas[market]:
            return _result(False, f"market {market + 1} buys {y} umbrellas but only {umbrellas[market]} exist")
        load[market] += x
        load[market + 1] += z
        spent += y
    if spent != cost:
        return _result(False, f"umbrellas bought ({spent}) differ from the stated cost ({cost})")
    for shelter in range(n):
        if load[shelter] > shelters[shelter]:
            return _result(False, f"shelter {shelter + 1} holds {load[shelter]} but fits {shelters[shelter]}")
    return _result(True, f"YES with cost {cost}")
