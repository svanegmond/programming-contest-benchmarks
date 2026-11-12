# Runs

Finished runs are published as a GitHub Pages site, not committed to `main`. `main` holds code, problem data, tests and docs.

Raw output (code, transcripts, prompts) stays in `runs-raw/`, which is gitignored. A published page never includes solutions.

## What a published report contains

`python -m waterloo publish RUN_ID --site site/` writes:

- `index.html`: a short table with pass@k (correctness), mean `partial_score`, and on-time / over-time counts vs the statement limit, split by language.
- `run-config.json`: downloadable reproducibility payload (model, provider, effort, token limits, temperature, k, lane, selection method, prompt version, HTTP timeout, 120s per-test give-up timeout, git SHA of this harness, env id, cohort hash).
- `results.csv`: one row per attempt, including `language`, `verdict`, `outcome`, `partial_score` and `cost_usd`.
- `summary.md`: the same table in Markdown.

Several runs can share one site: `python -m waterloo publish RUN_A RUN_B --note "..." --site site/` writes each run under `site/RUN_ID/` and an overview `index.html` with one row per attempt (outcome, tests passed, output tokens, time, cost, sync or batch) plus the notes.

For Batch API runs, `run-config.json` also lists `batch_models` and `batches` (job id, provider, submitted and completed time, latency, request counts, usage and cost).

Host names, SSH targets, code, transcripts and prompts are omitted.

## How a run lands on Pages

1. `python -m waterloo run --models openrouter/... --cohort cohorts/mine.json --mode single_shot` from a clean checkout. Add `--dry-run` to resolve config without calling a model.
2. `python -m waterloo report RUN_ID` prints the table locally.
3. `python -m waterloo publish RUN_ID --site /tmp/cco-pages`.
4. Push `/tmp/cco-pages` to the `gh-pages` branch (orphan branch, not `main`). `.github/workflows/pages.yml` deploys that branch; Actions reads the workflow from the pushed branch, so keep a copy of it on `gh-pages` too.

Do not commit `site/` or `runs-raw/` to `main`.
