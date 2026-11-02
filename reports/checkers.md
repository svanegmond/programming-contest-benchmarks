# Checkers: validators and partial scorer

Three validators pass admission. The scorer implements partial scoring but stays rejected: 25 official sample outputs contradict the statement's route-length definition.

`waterloo-suite admitted=51 rejected=21 (interactive=1,needs_scorer=2,needs_validator=8,output_only=1,samples_disagree=7,unclear=2) pending=0`


| Problem | Official outputs accepted | Truncated rejected | One value changed rejected | Constraint violated rejected | Admission |
|---|---:|---:|---:|---:|---|
| cco-2017-d2p3 Shifty Grid | 42/42 | 12/12 | 12/12 | 12/12 | admitted |
| cco-2022-d1p2 Rainy Markets | 140/140 | 12/12 | 10/10 | 12/12 | admitted |
| cco-2024-d1p2 Pizza Party | 87/87 | 12/12 | 12/12 | 12/12 | admitted |
| cco-2020-d2p1 Travelling Salesperson | 98/123 | 12/12 | 12/12 | 10/10 | rejected, needs_scorer |

Every category needs at least 3 rejected tests and no checker crashes. The mutation pool contains the 6 smallest inputs plus seeded random tests, 12 total. A `NO` output has no numeric value to change; a two-building route has no interior stop to delete. All generated mutants were rejected, with no crashes. The figures above are also summarised, counts only, in each problem's `checker-proof.md`. The proof script (`prove_checker.py`) and the raw `checker-proof.json` files are not in this repository.

## Semantics

- **2017 d2p3:** simulate row and column shifts, require valid indices and amounts, at most 100,000 moves, and a solved grid. Do not compare against the jury's move sequence. The statement says K >= 1, but official outputs use K=0 for already-solved grids; accept 0 only when the grid is solved.
- **2022 d1p2:** compare only the fixed YES/NO answer and minimum cost. Validate nonnegative market splits, population totals, umbrella stock, shelter capacities, and equality of purchased umbrellas to the printed cost.
- **2024 d1p2:** compare only minimum K (or -1). Simulate pizza placement and pickup, requiring the requested flavour at the top of the selected stack. This matches the official testlib checker.
- **2020 d2p1:** validate each route's starting building, roads, coverage, and at most one colour change. For optimal length K and submitted length M, score 0 if M > 2K, 25 if M = K, otherwise `8 + (8*(2*K-M)) // (K-1)`. Return the minimum plan score divided by 25. Invalid routes zero the test. The problem has one subtask of weight 1, so the judge's minimum test score implements the statement's minimum over all tests.

All 149,599 jury routes across the 98 graded 2020 inputs have length N. Visiting N distinct buildings requires at least N visits, so every reference optimum is proven. The proof rejects unverified longer reference routes; small cases can use an exact BFS fallback.

The 2020 statement's sample receives the hand-computed 13/25: lengths 5, 6, 5, 4 against K=4 give plan scores 16, 13, 16, 25. Padding valid routes with two bounce steps gives partial scores on 12 mutation tests.

**Scorer admission blocker:** the official ZIP contains 25 extra pairs in `day2data/cco_d2p1/samp/`. In each pair the output gives M=1 for a two-building input, and M=1 cannot describe a two-building route under the statement. Accepting those outputs would weaken the checker; omitting them would violate the requested EVERY-official-output admission rule. The proof includes and rejects all 25. Admission needs corrected official data or an explicit decision permitting their exclusion; this branch does neither.

## Judge and admission wiring

`judge/checkers.py` loads `problems/<id>/checker.py`. Its interface is `check(input_text, expected_text, output_text) -> {ok, score, message}`. Non-UTF-8 output and checker exceptions fail safely with score 0. Missing checker files fail fast.

**Checkers run as plain Python in the judge process on the judge host, not inside the VM.** Submission code still runs inside VM/isolate. `grade()` writes `checker.json` with `runs_in: host_python`, submission backend, checker SHA-256, and per-test messages. `TestResult.score` is an additive optional field in `cco_eval/waterloo.py` and the guest fallback dataclass. Scorers retain fractional scores even when `ok` is false; runtime errors and kills score 0. Subtask scores use their minimum test score.

The import is lazy because the guest imports `core.py` as a top-level module for sandbox operations. The VM image was not rebuilt; its env_id is unchanged. The host's grading code loads the new checkers.

`admit.py` reads `problems/<id>/checker-proof.md` and requires `Conclusion: admitted`, all official outputs accepted, and at least 3 mutation rejections per category with no accepted mutants and no crashes. A missing or unreadable summary means the checker is unproven and the problem stays rejected; it is never an error. The summary carries no hashes, so it is not bound to the exact `checker.py` and `tests.jsonl` it was recorded against. Passing checker proofs allow input-only statement-sample matching. Failed proofs remain rejected and appear in status evidence.

`map_2017_d2p3_subtasks.py` derives the missing subtask map from statement properties: N*M <= 8 gives 10 tests, solvable in at most 2 shifts gives 11, full constraints gives 19. These groups independently match contiguous test-number ranges.

