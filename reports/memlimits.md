# Memory limits from DMOJ

## Result

60 problems had `memory_limit_mb: null` (not 66: the 6 CCO 2026 statements already give a limit). All 60 now have one, 59 matched by exact normalized title and 1 by a reviewed alias. 0 unmatched.

Resulting `problem.json` values: 54 problems at 1024 MB and 6 at 256 MB (the 2025 set) with `memory_limit_source: "dmoj:<code>"`; the 6 statement-sourced 2026 limits are unchanged.

## Method

`code/cco-eval/fetch_dmoj_limits.py` lists `https://dmoj.ca/api/v2/problems` (all pages), keeps codes shaped `ccoYYpN` (drops `ccoprep*`, `ccoqr16*`, `*online`, `*hard`, lab problems), and matches each null-limit problem to a DMOJ problem **in the same year by normalized title**, never by number. It then reads `/api/v2/problem/<code>` and converts `memory_limit` (KB) to MB. Output: `env-study/dmoj-memory-limits.json` (`problem_id`, `title`, `dmoj_code`, `dmoj_name`, `memory_limit_mb`, `url`, `match`, plus `why` when unmatched). No timestamps, sorted keys.

Numbering differs from the statements as the brief warned, for example statement `cco-2019-d1p2` Sirtet is DMOJ `cco19p2`, but `cco-2019-d2p1` Card Scoring is `cco19p4`. Title matching handles this.

### The one alias

`cco-2017-d2p1` is titled "Rainfall Capture" in the statement and "Rainfall Storage" on DMOJ (`cco17p4`). It was the only 2017 problem without an exact title match, and DMOJ's other five 2017 problems all matched the other five statements. Time limit agrees (1 s) and the statement describes the same rain-in-pillars task. It is hard-coded in `TITLE_ALIASES` and tagged `"match": "alias"` in the JSON.

## admit.py

`admit.py` loads `dmoj-memory-limits.json` (default: beside `--requirements`; override with `--dmoj-limits`). When the statement gives no limit and the file has a matched entry, it sets `memory_limit_mb` and `memory_limit_source: "dmoj:<code>"`. A statement limit always wins.

Behaviour change worth reviewing: `problem.json` used to be written only for problems without a worker `status.json`, so all 66 worker-handled problems kept a stale `problem.json`. It is now rewritten for every problem (it is derived data; `status.json` and `subtask_map.json` stay worker-owned). The regeneration diff touched only the two memory lines in 60 files, so nothing else was stale.

