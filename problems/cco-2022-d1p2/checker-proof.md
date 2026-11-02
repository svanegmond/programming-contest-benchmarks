# Checker proof summary: cco-2022-d1p2 (Rainy Markets)

Conclusion: admitted

Date: 2026-10-29. Recovered from the private working repository at commit `917c6c7`; the original `checker-proof.json` and `prove_checker.py` are not part of this repository.

## What was checked

- Grading kind: validator. The checker is `checker.py` in this folder.
- Every official expected-output file, including the extra sample pairs in the test-data ZIP, was fed to the checker as if it were a submission. All must be accepted with full score.
- Mutated copies of official outputs must be rejected, with no checker crashes and at least 3 rejections per mutation type.
- Mutation inputs: up to 12 tests per mutation type, made of the 6 smallest-input tests plus seeded random tests (seed 20261020). Types that did not apply to a test were skipped, so some counts are below 12.

## Counts

| Check | Result |
|---|---|
| Official outputs accepted | 140 of 140 |
| Official outputs rejected | 0 |
| Mutation `change_one_value` rejected | 10 of 10 tried (accepted 0, crashes 0) |
| Mutation `truncate_tail` rejected | 12 of 12 tried (accepted 0, crashes 0) |
| Mutation `violate_constraint` rejected | 12 of 12 tried (accepted 0, crashes 0) |
| Statement-sample outputs probed and valid | 4 of 4 |

## Mutation types

- `truncate_tail`: keep only the first half of the output lines.
- `change_one_value`: add one to one numeric token.
- `violate_constraint`: claim YES with a zero cost for a NO case; otherwise add one to one market's split.

## Result

All checks passed with no failures. Admitted.

## Reproduction

The proof script is not in this repository. From the private repository at commit `917c6c7`:

```sh
python3 code/cco-eval/prove_checker.py --problem-id cco-2022-d1p2 \
  --problems-root benchmarks/waterloo/problems --zips-root <zips_dir> \
  --corpus <cemc_resources_dir> \
  --requirements benchmarks/waterloo/env-study/problem-requirements.jsonl
```

It exits 0 and writes `checker-proof.json` beside the problem. Two full runs of this command in the private repository produced identical files (recorded in its checker report). Neither this summary nor that claim has been re-run from this repository, because the ZIPs are not vendored here and the script was removed.

## Applies to

`checker.py` and `tests.jsonl` as published here are byte-identical to the files the proof was recorded against.
