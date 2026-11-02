# Waterloo pilot rerun (pilot2)

Supersedes the scores of an earlier pilot that capped output at 16,000 tokens (diagnosed in `pilotfix.md`). Run ID `pilot2`, single-shot, k=1, env ID `687bded99a1810ec00310a4c21a4dad52197abf869a07c61ec594f4cfee9d9f9` (unchanged from the old pilot), caps 32,768 (GPT-OSS) and 65,536 (Gemini), 300 s request timeout, both models on the same 13 problems.

## Cohort

`pilot2-cohort.json` is `cohort.json` minus `cco-2020-d2p1`, which is `rejected` (needs a scorer). The other 13, including the three validator problems `cco-2017-d2p3`, `cco-2022-d1p2`, `cco-2024-d1p2`, are `admitted` and graded. `cco-2020-d2p1` has one stale run-only attempt, left under `runs/pilot2/20261029T182806Z/`.

## Results (13 graded problems per model)

| Model | On-time | Over-time | Truncated | Fail |
|---|---:|---:|---:|---:|
| `openrouter/google/gemini-3.8-flash` | 6 | 0 | 0 | 7 |
| `openrouter/openai/gpt-oss-20b` | 0 | 1 | 0 | 12 |

From `python -m waterloo report pilot2 --out runs/pilot2`. All 26 attempts ended with `finish_reason=stop`, so none was truncated at the raised caps.

| | Language | Failure kinds among the fails |
|---|---|---|
| Gemini | C++ 12, Python 1 | 2 compile errors (`cco-2017-d1p3`: `uint64_t` without `<cstdint>`; `cco-2021-d1p3`: a code fragment with no function), 5 wrong answers on some tests (`2018`, `2023-d1p2`, `2023-d1p3`, `2023-d2p1` at 0.72, `2025-d2p3` at 0.16) |
| GPT-OSS | Python 4, C++ 3, none 6 | 6 `no_program` (normal stop, no code; `2018-d1p3`, `2023-d1p2`, `2023-d2p1` are the refusals diagnosed in `pilotfix.md`), 6 executed fails with wrong answers, 4 of them (`2016-d2p3`, `2019-d1p3`, `2024-d1p2`, `2025-d2p3`) also had killed tests; `2024-d1p2` scores 0.36 |

Gemini's passes: `2016-d2p2`, `2016-d2p3`, `2017-d2p3`, `2019-d1p3`, `2022-d1p2`, `2024-d1p2`. GPT-OSS's one pass is `2016-d2p2` over time.

## Side by side with the superseded pilot

The old pilot (run `20261029T163913Z`) graded 10 deterministic problems per model with a 16,000-token cap and a stale checker-pending cohort.

| Model | Superseded pilot (10 deterministic) | pilot2 (13 graded) |
|---|---|---|
| Gemini | 1 pass / 9 fail; 13 of its 14 attempts had no program, nearly all `length` stops | 6 on-time / 7 fail; 0 truncated, 0 no-program |
| GPT-OSS | 1 pass / 9 fail; 3 refusals among the 10 | 0 on-time, 1 over-time / 12 fail; 6 no-program |

Gemini's old score came from the token cap. GPT-OSS did not change materially.

## Cost

| Model | Attempts | Input tokens | Output tokens | Reasoning tokens | Reported cost | Mean generation latency |
|---|---:|---:|---:|---:|---:|---:|
| Gemini | 13 | 14,704 | 383,944 | 317,716 | $1.450818 | 213.9 s |
| GPT-OSS | 13 | 14,966 | 26,654 | 20,127 | $0.003667 | 33.5 s |

