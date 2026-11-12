# waterloo-acbdc662ab0b619d

mode `single_shot` · lane `cpp` · k `1` · selection `pass_at_k` · effort `high` · temp `0.0` · prompt `cco-v3` · git `d0e5ca275aaef8f9a0f4ddd353352be1026d4eca`

pass@k is correctness (`all_pass`). On-time / over-time are vs the statement limit, not the score.

| Model | Language | pass@k | mean partial_score | On-time | Over-time | Truncated | Fail |
|---|---|---:|---:|---:|---:|---:|---:|
| `openrouter/anthropic/claude-sonnet-5.5` | all | 1 | 1 | 1 | 0 | 0 | 0 |
| `openrouter/anthropic/claude-sonnet-5.5` | cpp | 1 | 1 | 1 | 0 | 0 | 0 |
| `openrouter/google/gemini-4-argon` | all | 1 | 1 | 1 | 0 | 0 | 0 |
| `openrouter/google/gemini-4-argon` | cpp | 1 | 1 | 1 | 0 | 0 | 0 |
| `openrouter/google/gemini-4-carbon` | all | 0 | 0 | 0 | 0 | 0 | 1 |
| `openrouter/google/gemini-4-carbon` | none | 0 | 0 | 0 | 0 | 0 | 1 |

[Download run-config.json](run-config.json)

