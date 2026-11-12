# Runs

Finished runs are published as a GitHub Pages site, not committed to `main`. `main` holds code, problem data, tests and docs.

Raw output (code, transcripts, prompts, full reasoning traces) stays in `runs-raw/`, which is gitignored. A published page never includes solutions. Reasoning traces are published with fenced code removed (see below).

## What a published report contains

`python -m waterloo publish RUN_ID --site site/` writes:

- `index.html`: a short table with pass@k (correctness), mean `partial_score`, and on-time / over-time counts vs the statement limit, split by language.
- `run-config.json`: downloadable reproducibility payload (model, provider, effort, token limits, temperature, k, lane, selection method, prompt version, HTTP timeout, 120s per-test give-up timeout, git SHA of this harness, env id, cohort hash).
- `results.csv`: one row per attempt, including `language`, `verdict`, `outcome`, `partial_score` and `cost_usd`.
- `summary.md`: the same table in Markdown.
- `reasoning/<problem>--<model>--s<k>.md` and `.json`: each attempt's reasoning trace, redacted. `index.html` and `summary.md` show, for each model and problem, the trace kind, visible characters against billed reasoning tokens, and an excerpt of the first and last ~600 characters, with links to both files. `run-config.json` lists the traces under `reasoning_traces` and records `reasoning_published`.

Hidden and encrypted traces are labelled plainly: "Hidden: N reasoning tokens were billed but none were returned", "Encrypted: the provider returned only an opaque blob, nothing readable", or "Summary only".

Before a trace is written to the site it is redacted and cleaned:

- API keys (OpenRouter, Anthropic, OpenAI, GitHub, AWS, Google, Slack), JWTs, bearer tokens, `token=`/`password=`-style assignments and private-key blocks are replaced with `[REDACTED]`.
- Host names are redacted too: `*.local`, `*.lan`, `*.internal`, `*.home`, `*.corp`, `*.ts.net`, private and Tailscale IPv4 addresses, and `user@host` targets. So are the exact values of `WATERLOO_SSH_HOST`, `OLLAMA_URL`, `ANTHROPIC_BASE_URL` and `OPENROUTER_BASE_URL`, this machine's host name, any environment variable whose name contains KEY, TOKEN, SECRET, PASSWORD or CREDENTIAL, and the comma-separated strings in `CCO_REDACT`.
- Fenced code blocks are replaced with `[code block omitted from the published trace: N lines]`.
- Encrypted blobs are reduced to their length, and signatures are dropped.

The JSON copy records how many redactions and omitted code blocks it has. Redaction uses patterns, so read a trace before publishing one that touches infrastructure. `publish --no-reasoning` leaves the `reasoning/` folder, the excerpts and the overview's Reasoning column out.

Several runs can share one site: `python -m waterloo publish RUN_A RUN_B --note "..." --site site/` writes each run under `site/RUN_ID/` and an overview `index.html` with one row per attempt (outcome, tests passed, output tokens, time, cost, sync or batch) plus the notes. The overview has a Reasoning column with each attempt's trace kind, linked to its published trace. It reads "not recorded" for runs made before trace capture.

For Batch API runs, `run-config.json` also lists `batch_models` and `batches` (job id, provider, submitted and completed time, latency, request counts, usage and cost).

Host names, SSH targets, code, transcripts and prompts are omitted.

## How a run lands on Pages

1. `python -m waterloo run --models openrouter/... --cohort cohorts/mine.json --mode single_shot` from a clean checkout. Add `--dry-run` to resolve config without calling a model.
2. `python -m waterloo report RUN_ID` prints the table locally.
3. `python -m waterloo publish RUN_ID --site /tmp/cco-pages`.
4. Push `/tmp/cco-pages` to the `gh-pages` branch (orphan branch, not `main`). `.github/workflows/pages.yml` deploys that branch; Actions reads the workflow from the pushed branch, so keep a copy of it on `gh-pages` too.

Do not commit `site/` or `runs-raw/` to `main`.
