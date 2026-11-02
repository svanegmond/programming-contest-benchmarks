# programming-contest-benchmarks

A harness and an admitted problem set for evaluating language models on Canadian Computing Olympiad (CCO) problems. It covers the 12 CCO contests from 2015 to 2026. The official CEMC test data decides every verdict.

Model-written solutions, transcripts and prompts are not published. A solution or prompt on the public web can end up in a future model's training data and make later evaluations of that model meaningless. Published runs contain identifiers, verdicts, outcomes and costs only (see [docs/runs.md](docs/runs.md)).

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

A run draws a seeded subset, a cohort. `cohorts/cohort.json` is the seed 20261020 draw (10 deterministic and 4 unusual problems). `cohorts/pilot2-cohort.json` is the 13 graded problems of the first test run.

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
- `OPENROUTER_API_KEY` for model calls. Keep keys in the environment, never in a file.

```sh
python -m waterloo cohort --out cohorts/mine.json --deterministic 2 --unusual 0
python -m waterloo run --models openrouter/openai/gpt-oss-20b --cohort cohorts/mine.json --mode single_shot
python -m waterloo report RUN_ID
```

`run` writes raw output (code, transcripts, prompts) to `runs-raw/`, which is gitignored. Reduce a finished run to its publishable record with `code/cco-eval/export_run.py`; [docs/runs.md](docs/runs.md) has the steps.

## Layout

| Path | Contents |
|---|---|
| `problems/` | One folder per problem, admitted or rejected: `problem.json`, `status.json` (with the reason), `tests.jsonl`, `subtask_map.json` where the ZIP needs one. Four problems also have `checker.py` and `checker-proof.md`, a count-only summary of the checker proof. |
| `statements/` | CEMC contest HTML and PDFs, unmodified, with `manifest.json`. |
| `testdata/` | `zips.json`: URL and size of each official ZIP. |
| `cohorts/` | Seeded cohort files. |
| `runs/` | One folder per published run. |
| `reports/` | Methodology and aggregate results only. |
| `env-study/`, `jev-calibration/`, `agreement/`, `timing/` | Admission inputs, the Jev judge calibration results, the subtask-map agreement data, and the VM timing data. |
| `code/cco-eval/` | Admission, judge, runners, platform sinks, dashboard, cohort builder and tests. `waterloo/` is the command line. |

## License

CC BY-NC 4.0, see [LICENSE](LICENSE). The CEMC material stays the property of its owner: see [NOTICE](NOTICE).
