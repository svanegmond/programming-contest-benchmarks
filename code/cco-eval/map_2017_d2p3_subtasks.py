#!/usr/bin/env python3
"""Write subtask_map.json for cco-2017-d2p3 (Shifty Grid).

The ZIP names tests 03..42 with no subtask markers, and the statement defines
subtasks by property. Classify each input by property: N*M <= 8 is subtask_1;
otherwise solvable in at most 2 shifts is subtask_2; everything else is
full_constraints. sample01/sample02 are excluded.
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

ZIP = "2017/2017CCOTestData.zip"
DIRECTORY = "day2/shifty/"


def shift(grid: list[int], rows: int, cols: int, kind: int, line: int, amount: int) -> list[int]:
    out = grid[:]
    if kind == 1:
        start = line * cols
        row = out[start : start + cols]
        out[start : start + cols] = row[-amount:] + row[:-amount] if amount else row
    else:
        column = out[line::cols]
        out[line::cols] = column[-amount:] + column[:-amount] if amount else column
    return out


def solvable_in_one(grid: list[int], rows: int, cols: int) -> bool:
    solved = list(range(rows * cols))
    wrong = [index for index in range(rows * cols) if grid[index] != index]
    if not wrong:
        return True
    lines = []
    if len({index // cols for index in wrong}) == 1:
        lines.append((1, wrong[0] // cols, cols))
    if len({index % cols for index in wrong}) == 1:
        lines.append((2, wrong[0] % cols, rows))
    return any(
        shift(grid, rows, cols, kind, line, amount) == solved
        for kind, line, size in lines
        for amount in range(1, size)
    )


def solvable_in_two(grid: list[int], rows: int, cols: int) -> bool:
    if solvable_in_one(grid, rows, cols):
        return True
    wrong = [index for index in range(rows * cols) if grid[index] != index]
    # Shifting a line with no misplaced tile only breaks it, so both shifts use lines through misplaced
    # tiles. After the first shift, the tiles it did not touch must already lie on one line.
    candidates = [(1, row, cols) for row in {index // cols for index in wrong}]
    candidates += [(2, col, rows) for col in {index % cols for index in wrong}]
    for kind, line, size in candidates:
        rest = [index for index in wrong if (index // cols if kind == 1 else index % cols) != line]
        if len({index // cols for index in rest}) > 1 and len({index % cols for index in rest}) > 1:
            continue
        for amount in range(1, size):
            if solvable_in_one(shift(grid, rows, cols, kind, line, amount), rows, cols):
                return True
    return False


def classify(text: str) -> str:
    fields = text.split()
    rows, cols = int(fields[0]), int(fields[1])
    grid = list(map(int, fields[2:]))
    if rows * cols <= 8:
        return "subtask_1"
    return "subtask_2" if solvable_in_two(grid, rows, cols) else "full_constraints"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zips-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    members, excluded = [], []
    with zipfile.ZipFile(args.zips_root / ZIP) as archive:
        for name in sorted(info.filename for info in archive.infolist() if info.filename.startswith(DIRECTORY) and info.filename.endswith(".in")):
            if "sample" in name:
                excluded.append({"member": name, "reason": "statement sample"})
                continue
            members.append({"input": name, "expected": name[:-3] + ".a", "subtask": classify(archive.read(name).decode())})
    document = {
        "problem_id": "cco-2017-d2p3",
        "zip": ZIP,
        "method": "classified by statement property from each input grid: N*M<=8 -> subtask_1; solvable in <=2 shifts -> subtask_2; else full_constraints; sample01/sample02 excluded",
        "members": members,
        "excluded": excluded,
    }
    args.out.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    counts = {name: sum(1 for item in members if item["subtask"] == name) for name in ("subtask_1", "subtask_2", "full_constraints")}
    print(counts)


if __name__ == "__main__":
    main()
