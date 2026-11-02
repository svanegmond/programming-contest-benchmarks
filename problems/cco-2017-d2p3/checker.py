"""Validator for CCO 2017 Day 2 Problem 3 (Shifty Grid).

Any sequence of at most 10^5 shifts that solves the grid is accepted. The
statement asks for K >= 1, but the official outputs use K = 0 for grids that
are already solved, so K = 0 is accepted when it is true.
"""

MAX_MOVES = 100_000


def _int(token: str) -> int | None:
    if len(token) > 12 or not token.isascii():
        return None
    body = token[1:] if token[:1] == "-" else token
    return int(token) if body.isdigit() else None


def _result(ok: bool, message: str) -> dict:
    return {"ok": ok, "score": 1.0 if ok else 0.0, "message": message}


def check(input_text: str, expected_text: str, output_text: str) -> dict:
    header = input_text.split(None, 2)
    rows, cols = int(header[0]), int(header[1])
    cells = input_text.split()[2:]
    grid = [int(token) for token in cells]
    if len(grid) != rows * cols:
        raise ValueError("input grid does not have N*M cells")

    tokens = output_text.split()
    if not tokens:
        return _result(False, "empty output")
    moves = _int(tokens[0])
    if moves is None or not 0 <= moves <= MAX_MOVES:
        return _result(False, f"K must be an integer in [0, {MAX_MOVES}]: {tokens[0][:20]!r}")
    if len(tokens) != 1 + 3 * moves:
        return _result(False, f"expected {1 + 3 * moves} tokens for K={moves}, got {len(tokens)}")

    for move in range(moves):
        kind, line, amount = (_int(token) for token in tokens[1 + 3 * move : 4 + 3 * move])
        if kind == 1 and line is not None and amount is not None and 1 <= line <= rows and 0 <= amount < cols:
            start = (line - 1) * cols
            if amount:
                row = grid[start : start + cols]
                grid[start : start + cols] = row[-amount:] + row[:-amount]
        elif kind == 2 and line is not None and amount is not None and 1 <= line <= cols and 0 <= amount < rows:
            if amount:
                column = grid[line - 1 :: cols]
                grid[line - 1 :: cols] = column[-amount:] + column[:-amount]
        else:
            return _result(False, f"move {move + 1} is malformed or out of range")

    if grid != list(range(rows * cols)):
        return _result(False, "grid is not solved after the moves")
    return _result(True, f"solved in {moves} moves")
