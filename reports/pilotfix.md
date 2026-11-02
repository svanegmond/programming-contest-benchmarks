# Waterloo pilot failure diagnosis and runner fix

## Diagnosis from the saved pilot

Source run: `runs/pilot/20261029T163913Z/`. The old `run.json` records `max_tokens=16000`; the old `Attempt.generation` records stop reason and usage but not the request limit. The public OpenRouter model catalog reports `max_completion_tokens=65536` for `google/gemini-3.8-flash` and `32768` for `openai/gpt-oss-20b`. Thus the pilot's configured 16,000-token cap, not either model's catalog maximum, constrained the near-cap responses.

The table includes every saved attempt that had no extracted program. Token counts are `output_tokens / reasoning_tokens`; `-` means the provider omitted a reasoning-token count. All Gemini rows have `stop_reason=length`; GPT-OSS rows have `stop_reason=stop`.

| Model | Problem | Cohort | Output / reasoning tokens | Cause from metadata and raw response |
|---|---|---|---:|---|
| Gemini 3.8 Flash | `cco-2016-d2p3` | deterministic | 15,996 / 15,358 | Request cap; reasoning used 96% of output. Remaining visible text is analysis, with no code fence. Not an extractor miss. |
| Gemini 3.8 Flash | `cco-2017-d1p3` | deterministic | 15,996 / 2,858 | Request cap; raw response is 32,639 characters of visible analysis and has no fences or program. Model spent the remaining budget explaining rather than submitting code. |
| Gemini 3.8 Flash | `cco-2017-d2p3` | unusual | 15,959 / 15,959 | Request cap consumed entirely by reasoning; saved visible response is empty. |
| Gemini 3.8 Flash | `cco-2018-d1p3` | deterministic | 15,944 / 15,944 | Request cap consumed entirely by reasoning; saved visible response is empty. |
| Gemini 3.8 Flash | `cco-2019-d1p3` | deterministic | 15,359 / 15,359 | Request cap consumed entirely by reasoning; saved visible response is empty. |
| Gemini 3.8 Flash | `cco-2020-d2p1` | unusual | 15,946 / 15,946 | Request cap consumed entirely by reasoning; saved visible response is empty. |
| Gemini 3.8 Flash | `cco-2021-d1p3` | deterministic | 15,976 / 15,976 | Request cap consumed entirely by reasoning; saved visible response is empty. |
| Gemini 3.8 Flash | `cco-2022-d1p2` | unusual | 15,566 / 15,566 | Request cap consumed entirely by reasoning; saved visible response is empty. |
| Gemini 3.8 Flash | `cco-2023-d1p2` | deterministic | 2,711 / 2,711 | `length` arrived well below the 16,000 request cap; all recorded output was reasoning and the visible response is empty. This is an early model/provider stop, not a parser failure; the saved metadata does not expose why it stopped early. |
| Gemini 3.8 Flash | `cco-2023-d1p3` | deterministic | 15,996 / 15,360 | Request cap; visible response is analysis without a program or fence. Reasoning used 96% of output. Not an extractor miss. |
| Gemini 3.8 Flash | `cco-2023-d2p1` | deterministic | 15,996 / 15,358 | Request cap; visible response is analysis without a program or fence. Reasoning used 96% of output. Not an extractor miss. |
| Gemini 3.8 Flash | `cco-2024-d1p2` | unusual | 15,996 / 15,358 | Request cap and reasoning-heavy output. Raw response contains an opening `cpp` fence but no closing fence. This was an extractor miss; the fixed parser recovers C++ from the unclosed fence. |
| Gemini 3.8 Flash | `cco-2025-d2p3` | deterministic | 15,996 / 15,358 | Request cap and reasoning-heavy output. Raw response begins `cpp\n#include...` without an opening fence. This was an extractor miss; the fixed parser recovers the C++ body. |
| GPT-OSS 20B | `cco-2018-d1p3` | deterministic | 1,791 / 1,598 | Model behavior, not a token cap: response is “I’m sorry, but I couldn’t find a correct solution within the time limit.” It stopped normally and emitted no program. |
| GPT-OSS 20B | `cco-2023-d1p2` | deterministic | 1,506 / 1,481 | Model refusal: “I’m sorry, but I can’t provide a solution to this problem.” Normal stop, far below cap; no parser miss. |
| GPT-OSS 20B | `cco-2023-d2p1` | deterministic | 874 / 867 | Model refusal: “I’m sorry, but I can’t provide a solution to this problem.” Normal stop, far below cap; no parser miss. |
| GPT-OSS 20B | `cco-2024-d1p2` | unusual | 1,076 / 1,055 | Model refusal: “I’m sorry, but I can’t provide a solution to this problem.” Normal stop, far below cap; no parser miss. |

The pilot's three deterministic GPT-OSS no-program attempts are the explicit refusal/model-behavior cases above; a fourth identical refusal occurred in the unusual cohort. None indicates a fence or extraction bug. For Gemini, 12 of 13 no-program responses reached 15,359-15,996 output tokens under the 16,000 cap. Reasoning consumed at least 96% of output in 11 of those 12; the remaining near-cap response used 2,858 reasoning tokens and spent the rest on visible analysis. The one below-cap response stopped at 2,711 reasoning tokens with `length`; its specific upstream cutoff is not recorded.

## Runner and extractor changes

- The default cap is now model-specific and matches the OpenRouter catalog: Gemini 3.8 Flash uses 65,536; GPT-OSS 20B uses 32,768. Other models default to 32,768. `--max-tokens` remains an explicit positive override. The actual cap is recorded as `Attempt.generation.max_tokens`; the run manifest records per-model caps too.
- Added additive `FailureKind` values to `Attempt`. A `length` stop is recorded as `failure_kind=truncated` in the attempt and no-program result. A missing program without a length stop is `no_program`. This preserves the brief's three-way graded `Outcome3` while keeping truncation distinct rather than representing its cause only as `fail`.
- The extractor now accepts a truncated recognized fence without a closing fence and a code response beginning with a bare language label when the following line looks like source code. Synthetic fixtures `code/cco-eval/tests/fixtures/waterloo/gemini-2024-unclosed-fence-response.txt` and `gemini-2025-truncated-response.txt` reproduce those two truncation shapes on a trivial problem; tests cover both parser cases. The original saved responses are model-written solutions to contest problems and are not published.

