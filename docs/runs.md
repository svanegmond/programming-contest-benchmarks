# Runs

`runs/` holds one folder per published run, named `<YYYY-MM-DD>-<name>`. A run that only exercises the harness carries `label: test` and a `-test` suffix. A test run is not a result. `runs/2026-10-29-pilot2-test/` is the first entry.

## What a run folder contains

Exactly three files:

- `manifest.json`: `run_id`, `label`, `models`, `mode`, `caps`, `budget`, `problem_ids`, `timeout_s`, and an optional `note`.
  - `caps` holds the per-model output-token caps and the judge limits.
  - `budget` is `null` for single-shot runs, and wall-clock seconds and turns for agentic runs.
- `results.csv`: one row per attempt, with the columns `problem`, `mode`, `model`, `sample`, `verdict` (`pass` or `fail`), `outcome` (`pass_on_time`, `pass_over_time`, `truncated` or `fail`), `partial_score` and `cost_usd`.
- `summary.md`: outcome counts and cost per model, for the cohort in the manifest.

A run folder never contains code, transcripts, prompts, workspaces, model output text, host names, endpoint URLs or dashboard links. Model-written solutions, transcripts and prompts stay out of the repository because published text can contaminate future evaluations.

## How a run lands

1. Run `python -m waterloo run ...` from a clean checkout. Raw output goes to `runs-raw/`, which is gitignored.
2. Reduce it: `python code/cco-eval/export_run.py runs-raw/<run_id> --out runs/<date>-<name> --label <test|pilot|full>`.
3. Check that `results.csv` has one row per problem, model and sample.
4. Cite cost per generation, from `cost_usd`. A provider balance delta lags the calls and is not a cost.
5. Commit `runs/<date>-<name>/` and nothing from `runs-raw/`.

`code/cco-eval/tests/test_runs_layout.py` checks the file set, the manifest keys, the CSV header and the absence of hashes for every folder in `runs/`.
