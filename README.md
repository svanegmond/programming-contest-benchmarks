# programming-contest-benchmarks

A harness and an admitted problem set for evaluating language models on Canadian Computing Olympiad (CCO) problems. It covers the 12 CCO contests from 2015 to 2026. The official CEMC test data decides every verdict.

Model-written solutions, transcripts, prompts and reasoning traces are not published by default. A solution, or a trace that describes one, can end up in a future model's training data if it is on the public web, and make later evaluations of that model meaningless. Published runs are GitHub Pages reports with identifiers, verdicts, `partial_score`, pass@k and a downloadable run-config (see [docs/runs.md](docs/runs.md)). They are not committed to `main`.

`run` always saves traces locally as `reasoning.json` and `reasoning.md` under `runs-raw/`. `publish --with-reasoning` adds redacted excerpts and full traces to the report. Use that only for problems whose solutions are already public, such as older contest years. Redaction strips secrets, host names and fenced code blocks, but prose and unfenced code can still reveal the algorithm.

## The cohort

All 72 problems (12 contests, 6 each) were put through admission. 51 are admitted and 21 are rejected, each with a closed-set reason in `problems/<id>/status.json`. [reports/DASHBOARD.md](reports/DASHBOARD.md) lists them all.

| Reason | Problems |
|---|---:|
| `samples_disagree` | 7 |
| `needs_validator` | 8 |
| `needs_scorer` | 2 |
| `unclear` | 2 |
| `interactive` | 1 |
| `output_only` | 1 |

A problem is admitted only when its statement sample matches a pair in the official ZIP, its tests map to the statement's subtasks, and its grading kind (exact, float tolerance, or a validator) is implemented. Validators for three problems are admitted.

A run draws a seeded subset, a cohort. `cohorts/cohort.json` is the seed 20261020 draw (10 deterministic and 4 unusual problems). `cohorts/pilot2-cohort.json` is a 13-problem subset used in early harness checks.

## Fetch the test data

The official test-data ZIPs are not in this repository (about 7.6 GB). `testdata/zips.json` lists the CEMC URL and byte size of each one. CEMC does not publish checksums, so files are fetched from their source URLs and not verified. Statements are vendored under `statements/`.

```sh
python3 code/cco-eval/fetch_zips.py --dry-run                  # list which ZIPs are present, download nothing
python3 code/cco-eval/fetch_zips.py --output-dir /path/to/zips # download every missing ZIP
```

The script exits nonzero if a download fails, and warns if a file's size differs from the manifest. A problem's `tests.jsonl` points at members as `<year>/<zip name>::<member path>`, relative to the ZIP directory.

## Run one benchmark

Install `pytest` from `pyproject.toml` (Python 3.12 or later), then run `make test`. A run needs two things outside this repository:

- A judge host reachable over SSH. It holds the fetched ZIPs and the pinned Arch Linux judge VM built by `code/cco-eval/judge/build-image.sh` and `judge/vm.sh`. Point at it with `WATERLOO_SSH_HOST` (`user@host`) and `WATERLOO_REMOTE_ROOT` (default `/srv/waterloo`; ZIPs go in `$WATERLOO_REMOTE_ROOT/zips`).
- `OPENROUTER_API_KEY` for model calls. Keep keys in the environment, never in a file. `--dry-run` resolves config without a key.

```sh
python -m waterloo cohort --out cohorts/mine.json --deterministic 2 --unusual 0
python -m waterloo run --models openrouter/openai/gpt-oss-20b --cohort cohorts/mine.json --mode single_shot
python -m waterloo report RUN_ID
python -m waterloo publish RUN_ID --site site/
```

`run` writes raw output (code, transcripts, prompts, reasoning traces) to `runs-raw/`, which is gitignored. `publish` omits traces unless you pass `--with-reasoning`. [docs/runs.md](docs/runs.md) has the Pages steps.

### Flags

| Flag | Default | Notes |
|---|---|---|
| `--mode` | required | `single_shot` or `agentic` |
| `--lane` | `cpp` | Headline C++ lane. `python` forces PyPy3. `free` lets the model pick; reports split by language and never take the better language per problem. Standard library only. |
| `--k` | `1` | Samples per problem. Reports unbiased pass@k (Chen et al. 2021) on correctness (`all_pass`) and mean `partial_score`. Statement time is recorded, not a pass/fail cut. |
| `--temperature` | `0` | Configurable; raise it for k>1 studies. |
| `--reasoning-effort` | `high` | Sent for every reasoning model and recorded. `none` omits the parameter. |
| `--max-tokens` | catalog | OpenRouter public catalog, with a 65,536 floor for reasoning models. Allowed in both modes. |
| `--timeout` | `7200` | HTTP transport timeout for dead connections (retried). Not a generation budget. There is no wall-clock limit on the model or the agent. |
| `--max-turns` | `20` | Agentic stopping condition. |
| `--cluster-select` | off | AlphaCode-style filter/cluster when k>1. Logs the chosen sample and why. Official tests are still graded for pass@k. |
| `--multi-submit` | off | Agentic: verdict-only feedback (`accepted` / `wrong_answer` / `too_slow` / `runtime_error` / `compile_error`), counting failed submits. `too_slow` means the 120s per-test give-up timeout killed the program, not “over the statement limit”. |
| `--batch` | off | Single-shot only: send every model through OpenRouter's Batch API. Same as naming a model `openrouter/<slug>:batch`. See below. |
| `--dry-run` | off | Write `run-config.json` without calling a model. With batch models it also prints the worst-case cost. |

Statement time limits are recorded on every test (`over_time`, `pass_on_time` / `pass_over_time`) and shown in reports. They are not a pass/fail cut: pass@k and `partial_score` use correctness. Each test is killed after 120 seconds so a runaway cannot hang the judge.

### Batch API (single-shot)

OpenRouter lists batch pricing (usually half the sync price) on `<slug>:batch` catalog ids. Those ids are not callable on `/chat/completions`; a batch goes to `POST /api/v1/batches` with the base slug. Name a model `openrouter/<slug>:batch`, or pass `--batch` to do that for every model. The `:batch` id is what the run records, and its catalog entry supplies the token cap, effort and price.

- One batch per model holds every unfinished sample of the cohort. The request bodies are the ones the sync path sends.
- Before submitting, the harness reads `GET /api/v1/credits` and refuses if the remaining balance is below the worst case: an over-estimated prompt plus `max_tokens` of output for every request, at the `:batch` price. Lower `--max-tokens` to fit.
- Polling has no wall-clock limit. It starts at 30 s, backs off to 5 min, waits 2 min on `429` and keeps going through `5xx`. `402`, `404` and `410` stop the run.
- The job id is written to `runs-raw/RUN_ID/batches/<model>.active.json` as soon as OpenRouter accepts the batch. Rerun the same command after an interruption and it resumes polling that job instead of resubmitting. Once its results are recorded the file is renamed `<model>.<batch_id>.json`.
- Requests the batch did not answer are not scored. The run exits non-zero and lists them; rerunning submits a new batch for just those.
- `attempt.json` records `generation.batch` (job id, submitted and completed time, latency, provider). Latency is submit to finalize. `run-config.json` on Pages lists the jobs with usage and cost.
- Agentic mode and `--cluster-select` reject batch models: an agent turn depends on the previous tool result.

### Reasoning traces

Every OpenRouter request that sends a reasoning effort sends `reasoning: {"effort": ..., "exclude": false}`, on both the sync and the batch path. OpenRouter returns reasoning by default unless `exclude` is true, so `exclude: false` only makes that explicit. With `--reasoning-effort none` no `reasoning` object is sent. The response's `message.reasoning` string (or its `reasoning_content` alias) and the `message.reasoning_details` array (`reasoning.text`, `reasoning.summary`, `reasoning.encrypted`) are kept for every attempt:

- `reasoning.json` holds the raw `reasoning` string and `reasoning_details` array per turn (one turn in single-shot, one per model call in agentic mode), plus the per-turn counts. Signatures and encrypted blobs are kept unchanged.
- `reasoning.md` holds the readable text, with a heading per turn in agentic mode.
- `attempt.json` has a `reasoning` block: `kind`, `visible_chars`, `visible_tokens_est` (characters / 4), `billed_reasoning_tokens` (`usage.completion_tokens_details.reasoning_tokens`), `turns`, `trace_path` and `text_path`. `generation.reasoning` (single-shot) and `generation.calls[].reasoning` (agentic) carry the same counts. `telemetry.jsonl` adds `reasoning_kind` and `visible_reasoning_chars`.

`kind` is one of these:

| Kind | Meaning |
|---|---|
| `full` | Readable reasoning text (`reasoning.text` items or the `reasoning` string). |
| `summary` | `reasoning.summary` items, or text that is under half the billed reasoning tokens. Claude's summarized thinking and Gemini thought summaries look like text but are condensed. `kind_basis` in `reasoning.json` says which rule applied. |
| `encrypted` | Only `reasoning.encrypted` blobs, with nothing readable. |
| `none` | Nothing returned. If reasoning tokens were billed, the report labels it hidden. |

Agentic mode saves each turn's reasoning but does not send `reasoning_details` back to the model on the next turn, so agent behaviour is the same as before trace capture.

Traces stay in the local run. They are published only with `--with-reasoning`, as described above.

A future `libraries` lane is not built. The agent would request packages, the harness would check an allowlist and install pinned versions offline for both the agent sandbox and the judge, and versions plus hashes would be recorded.

## Layout

| Path | Contents |
|---|---|
| `problems/` | One folder per problem, admitted or rejected: `problem.json`, `status.json` (with the reason), `tests.jsonl`, `subtask_map.json` where the ZIP needs one. Four problems also have `checker.py` and `checker-proof.md`, a count-only summary of the checker proof. |
| `statements/` | CEMC contest HTML and PDFs, unmodified, with `manifest.json`. |
| `testdata/` | `zips.json`: URL and size of each official ZIP. |
| `cohorts/` | Seeded cohort files. |
| `reports/` | Admission dashboard and judge/environment notes. Run scores are not stored here. |
| `env-study/`, `jev-calibration/`, `agreement/`, `timing/` | Admission inputs, the Jev judge calibration results, the subtask-map agreement data, and the VM timing data. |
| `code/cco-eval/` | Admission, judge, runners, platform sinks, dashboard, cohort builder and tests. `waterloo/` is the command line. |

## License

CC BY-NC 4.0, see [LICENSE](LICENSE). The CEMC material stays the property of its owner: see [NOTICE](NOTICE).
