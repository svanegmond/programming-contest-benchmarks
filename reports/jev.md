# Jev as a measured judge

Status: built, tested, and calibrated live on `jev-1.14.0` (24 calls, no errors). Results are in the "Live calibration" section.

## What exists

- `code/cco-eval/jev_judge.py`: judge, HTTP client, CLI.
- `code/cco-eval/jev_calibration.py`: calibration-set builder (stdlib only) and summary.
- `code/cco-eval/tests/test_jev_judge.py`: 19 tests.
- `jev-calibration/cases.jsonl`: 24 calibration cases, built locally by `jev_calibration.py prep` (not published).

Jev never decides an official verdict. It writes `jev_results.jsonl` rows `{attempt_id, test_id, p_valid, cause, jev_model, raw}`.

## Design (from docs.typesafe.ai)

- One request per (attempt, test) with named state fields: `problem_statement` (from `runners.common.load_problem`), `test_input`, `reference_output`, `model_output`.
- Two independent questions in that one request, as the fan-out guidance says:
  - `p_valid`: a Noul, the probability that `model_output` is a correct output.
  - `cause`: a Choice over `valid`, `wrong_format`, `violates_constraint`, `suboptimal`, `wrong_answer`, each with a criterion text.
- Both instructions say that when the statement accepts any correct output, a `model_output` that differs from `reference_output` can still be valid.
- The client calls `POST /v1/systemone` directly with urllib. The request and response shapes follow the published OpenAPI schema.
  - It retries 429, 5xx and 529 with backoff and honours `retry-after`.
  - It does not depend on the SDK, so the SDK's call signatures were never exercised.
- The model defaults to the pinned id `jev-1.14.0`, not the moving `jev-latest` alias (which points to 1.13 today). Override with `--model`.
- `raw` holds the full API response (answers, per-choice probabilities, usage) plus `latency_s`.
- Failures (HTTP error, malformed answer, oversized state) write a row with `p_valid` and `cause` null and `raw.error`. Reruns skip error-free rows and retry the errored ones.

### Known risks the docs flag for jev-1.13 (not re-measured for 1.14)

- "Does not count, struggles with numeric precision." Many failure modes here are numeric (cost off by 1, row sums). The calibration is meant to show whether Jev catches them.
- "Accuracy falls with large state." Default context is 32k tokens per question. The judge refuses states over 60,000 characters rather than send them.
- "Leans toward the first Choice option." `valid` is listed first, so `cause` may be biased toward `valid`. I did not reorder or double-ask.

## Calibration set

Built by `jev_calibration.py prep` from the official test-data ZIPs, which are fetched and not vendored. `cases.jsonl` holds excerpts of official tests, so it is generated locally and gitignored.

- The problems are `cco-2022-d1p2` (Rainy Markets) and `cco-2025-d2p2` (Patrol Robot). Both are any-valid.
- Each problem has 3 tests, chosen from smallest to largest among the tests that fit Jev's context (case size at most 20k characters) and whose corruptions can all be verified.
- Each test has 4 variants: the official expected output, plus three corruptions.

| Problem | Tests (input chars) |
| --- | --- |
| `cco-2022-d1p2` | `d1p2.2-39` (18), `d1p2.2-36` (22), `d1p2.4-104` (42) |
| `cco-2025-d2p2` | `d2p2.4-52` (28), `d2p2.2-35` (100), `d2p2.4-55` (8484) |

Only tiny Rainy Markets tests fit the context cap. Every other test has N in the thousands and its input is 25k to 88k characters. So Rainy Markets results say nothing about realistic sizes.

| Variant | Rainy Markets | Patrol Robot |
| --- | --- | --- |
| `expected` | official output | official output |
| `truncated` | first half of the lines | first half of the lines |
| `value_changed` | umbrella-cost line plus 1, rows unchanged | one segment endpoint changed so the painted graph is disconnected |
| `constraint_violated` | a market sends -1 people to shelter i, surplus to i+1, row sum kept | an extra `1 1` segment (the statement requires u != v), header M plus 1 |

Every corruption is checked by code before it is used (`ground_truth_verified`). A test is dropped when a corruption cannot be proven invalid. For example, the triangle test for Patrol Robot has no single-endpoint change that disconnects the graph. Checks run:

- Row sums equal the market populations.
- The cost equals the umbrella column sum.
- The graph is connected for the original and disconnected for the mutation.

The `expected` variant is valid by definition.

## Running the calibration (one command)

```
export TYPESAFE_API_KEY=...        # https://console.typesafe.ai/
cd code/cco-eval && python3 jev_judge.py --calibrate
```

Without the key it exits with code 2 (checked before the live run) and this message: `TYPESAFE_API_KEY is not set. Create a key at https://console.typesafe.ai/ and export it, then rerun.`

It writes `jev-calibration/jev_results.jsonl` and `summary.json`, and prints the summary. The summary has:

- the p_valid distribution per variant
- the cause counts per variant
- accept/reject counts at 0.5
- latency
- input tokens
- cost at $0.042 per million input tokens

## Live calibration (jev-1.14.0, 24 calls, 0 errors)

```
cd code/cco-eval && python3 jev_judge.py --calibrate     # 4 workers
```

Outputs: `jev-calibration/jev_results.jsonl` (24 rows) and `summary.json`.

### p_valid by variant (6 cases each: 2 problems x 3 tests)

| Variant | min | median | mean | max | at least 0.5 |
| --- | --- | --- | --- | --- | --- |
| `expected` (official output) | 0.89 | 0.955 | 0.945 | 0.99 | 6/6 |
| `truncated` | 0.03 | 0.05 | 0.063 | 0.15 | 0/6 |
| `value_changed` | 0.04 | 0.075 | 0.122 | 0.38 | 0/6 |
| `constraint_violated` | 0.03 | 0.055 | 0.057 | 0.09 | 0/6 |

At a 0.5 threshold Jev accepted 6 of 6 valid outputs and rejected 18 of 18 corrupted ones. The widest margin to the threshold is `value_changed` on `d2p2.4-55` (the 8.5k-character Patrol Robot test) at 0.38. Valid and invalid p_valid ranges do not overlap: the lowest valid is 0.89, the highest invalid is 0.38.

### Cause choice

| Variant | Expected cause | Matched | What Jev chose |
| --- | --- | --- | --- |
| `expected` | `valid` | 6/6 | `valid` x6 |
| `truncated` | `wrong_format` | 4/6 | `wrong_format` x4, `wrong_answer` x2 (both Patrol Robot, smallest tests) |
| `constraint_violated` | `violates_constraint` | 6/6 | `violates_constraint` x6 |
| `value_changed` | not a single right label | n/a | Rainy Markets (cost line plus 1): `suboptimal` x3. Patrol Robot (graph disconnected): `violates_constraint` x2, `wrong_answer` x1 |

- Exact-label accuracy on the three variants with an unambiguous label is 16/18 (89%).
- For `value_changed` I did not score a label. A cost that is 1 too high is arguably `suboptimal` or `wrong_answer`, and a disconnected graph is arguably `violates_constraint` or `wrong_answer`. Jev's picks are defensible in each case.

### Cost and latency

- Per call: median 0.149 s, mean 0.169 s, min 0.103 s, max 0.262 s. This is the single-request wall time inside 4 concurrent workers, and `latency_s` is stored in each row's `raw`.
- Tokens: 109,503 input tokens in total, 4,563 per call on average. Per call it ranges from 1,921 (tiny cases) to 17,691 (the large Patrol Robot test).
- Cost: $0.0046 for all 24 calls (about $0.00019 per call) at $0.042 per million input tokens.
- At this per-call average, judging 10,000 outputs would cost about $1.9.

### Live parsing

- The fake client hid no problems. The real response parsed on the first run. It is `{model, answers: {p_valid: {type, noul}, cause: {type, choice, confidence, probabilities}}, usage}`, which matches what the parser reads.
- `model` came back as `jev-1.14.0`, the pinned id.
- I added a captured real response as a fixture (`tests/fixtures/jev_live_response.json`) and a parser test on it. Tests now: 19 pass.
- `raw` already keeps the per-choice `probabilities` and `confidence`, so a later analysis can use them without rerunning.

### What this does not show

- The corruptions are blunt (truncation, a +1 cost, a self-loop). Cases like these are the easy end of the range. The hard question is whether Jev catches a plausible-looking wrong answer from a real model, and 24 synthetic cases cannot answer it.
- All the inputs are small. The biggest is 8.5k characters. Rainy Markets has nothing above 42 input characters. Jev's docs warn that accuracy falls with state size and that it does not do arithmetic reliably. Whether the clean separation holds at N in the thousands is untested.
- Only one Choice option order was used, with `valid` first. The docs say Jev leans toward the first option. Truncated cases picked `wrong_answer` over `wrong_format` twice, so the cause label is the noisier of the two outputs.
- No repeat runs, so there is no measure of run-to-run variation. The p_valid values come from the alias-free model `jev-1.14.0` and may move with later versions.
- Suggested next step: run the judge over real runner attempts on any-valid problems, next to the official verdicts, and measure agreement there.
- `Attempt`/`TestResult` integration is still open. The judge takes a JSONL of cases (`attempt_id, test_id, problem_id, input, expected, output`). Runners' stored outputs need a small adapter to produce those rows.

