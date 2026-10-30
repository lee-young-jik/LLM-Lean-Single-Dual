# Final Single/Dual pipeline

This document describes the full-memory two-goal runtime used by the 137-problem analysis.
The twelve critical runtime
files are recorded in [the source manifest](../configs/final_pipeline_source_sha256.json).
Hashes refer to UTF-8 files with LF line endings before checkout CRLF conversion.

## Roles and information flow

| Stage | Model | Inputs and output |
|---|---|---|
| Orchestrator | `openai/gpt-6-luna-pro` | Problem, available or assigned Lean goal, previous-round memory and Lean environment declarations; returns `task` and `lean_strategy`. |
| Natural | `openai/gpt-6-luna` | Problem and selected goal; produces one independent mathematical argument per round and lane. Receives neither the Orchestrator strategy nor historical memory. |
| Generator | `openai/gpt-6-luna` | Goal, strategy, Natural argument and Lean environment; returns one `lean_proof`. Retries also receive previous code, Lean diagnostics and Repair guidance. |
| Repair | `openai/gpt-6-luna` | Goal, strategy, argument, failed code and diagnostics with source context; returns only `retry_guidance` for Generator. |

Single receives both candidate goals and selects prove or disprove each round.
It can revise the direction using its own previous-round records.

Dual runs two fixed lanes for prove and disprove. Each plans for its assigned
goal and sees both lanes' **previous-round** records. The round snapshot barrier
prevents access to its sibling's current-round attempts. Fixed-direction prompts
require an active proof attempt and prohibit abandoning a lane merely because
an LLM judges the goal false. Only Lean verification establishes success; an
opposite lane's failure is not evidence.

Planner memory contains previous strategies, independently produced Natural
arguments and generated Lean code. It excludes compiler errors and Repair
guidance. Natural does not receive Planner strategy or history.

## Budgets and provider

Use [two_goal_experiment.env](../configs/two_goal_experiment.env). Each lane has
at most three rounds and three Lean attempts per round: Single at most nine,
Dual at most eighteen across both lanes. Dual lanes run concurrently and stop
further sibling attempts after a verified solution. Equal rounds do not imply
equal compute.

The provider is pinned to `openai/flex`, without silent fallback. Workers use
reasoning effort `none`; the Pro planner retains its default behavior. Output
caps are 16,384 tokens, or 2,048 for Repair. Current model-call timeouts, including Repair, are 900 s; Lean verification is limited to 300 s.

## Running and auditing

Follow [CURRENT_SETUP.md](CURRENT_SETUP.md) for setup and offline checks.
`run_logged_experiment.py` accepts `--modes single`, `--modes dual`, or both
(default). Specify `--memory-conditions full --repetitions 1` for a one-pass
default comparison. Generated modules use `MiniF2F.Scratch.Audit*`, matching
the pinned Lean project.

Local audits preserve agent inputs, instructions, outputs, API usage, Lean
diagnostics and source snapshots. Credentials are redacted; `.env` remains
local. Never pass reference proofs or evaluator labels to evaluation agents.

