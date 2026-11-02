# Checker proof summary: cco-2020-d2p1 (Travelling Salesperson)

Conclusion: rejected

Date: 2026-10-29. Recovered from the private working repository at commit `917c6c7`; the original `checker-proof.json` and `prove_checker.py` are not part of this repository.

## What was checked

- Grading kind: scorer. The checker is `checker.py` in this folder.
- Every official expected-output file, including the extra sample pairs in the test-data ZIP, was fed to the checker as if it were a submission. All must be accepted with full score.
- Mutated copies of official outputs must be rejected, with no checker crashes and at least 3 rejections per mutation type.
- Mutation inputs: up to 12 tests per mutation type, made of the 6 smallest-input tests plus seeded random tests (seed 20261020). Types that did not apply to a test were skipped, so some counts are below 12.

## Counts

| Check | Result |
|---|---|
| Official outputs accepted | 98 of 123 |
| Official outputs rejected | 25 |
| Mutation `change_one_value` rejected | 12 of 12 tried (accepted 0, crashes 0) |
| Mutation `truncate_tail` rejected | 12 of 12 tried (accepted 0, crashes 0) |
| Mutation `violate_constraint` rejected | 10 of 10 tried (accepted 0, crashes 0) |
| Statement-sample outputs probed and valid | 1 of 1 |
| Reference route lengths proven optimal | 149599 of 149599 plans across 98 tests (mismatches 0) |
| Padded valid routes scoring strictly between 0 and 1 | 12 of 12 |

## Mutation types

- `truncate_tail`: keep only the first half of the output lines.
- `change_one_value`: add one to one numeric token.
- `violate_constraint`: delete one interior stop from one plan's route.
- Partial-score probe: pad a valid route with a two-step bounce on its first edge.

## Result

Recorded failure: `25 official outputs rejected`. These are the extra sample pairs in the official ZIP whose outputs the scorer cannot accept. The scorer stays rejected (`needs_scorer`); it was not weakened to accept them.

## Reproduction

The proof script is not in this repository. From the private repository at commit `917c6c7`:

```sh
python3 code/cco-eval/prove_checker.py --problem-id cco-2020-d2p1 \
  --problems-root benchmarks/waterloo/problems --zips-root <zips_dir> \
  --corpus <cemc_resources_dir> \
  --requirements benchmarks/waterloo/env-study/problem-requirements.jsonl
```

It exits 1 and writes `checker-proof.json` beside the problem. Two full runs of this command in the private repository produced identical files (recorded in its checker report). Neither this summary nor that claim has been re-run from this repository, because the ZIPs are not vendored here and the script was removed.

## Applies to

`tests.jsonl` is identical to the file the proof was recorded against. `checker.py` here differs from the proved file by one comment edit (a reference to the removed script); the code is unchanged.
