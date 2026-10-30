# Final-paper reproducibility

This guide covers only the final manuscript's 137-problem Single/Dual analysis.

## 1. Recompute the published tables offline

Python 3.11+ is sufficient; no model credentials or paid calls are needed:

```bash
python3 scripts/validate_release.py
python3 scripts/reproduce_paper_tables.py --check
python3 scripts/reproduce_paper_tables.py --output /tmp/final-paper-tables.json
```

The script checks 1,370 execution records, successful proof-body lengths, dataset
alignment and recorded direction choices. It reproduces tables 1, 4, 5, 6 and 7.
Tables 2 and 3 describe budgets/models, not statistical aggregates.

`results/final_paper/executions.jsonl` contains sanitized execution outcomes,
successful Lean proof bodies, token counts, elapsed times and reported costs.
`single_choices.jsonl` supplies the Single direction choices discussed in Section 5.3.
`tables.json` is the expected aggregate output; `manifest.json` records file hashes
for the archived records. These are evaluator-only artifacts.

## 2. Definitions used in the final manuscript

- **Pass@5**: problems solved at least once in five complete executions / 137.
- **Accuracy@5**: the manuscript's name for mean per-execution accuracy;
  total correct executions / (137 × 5). It is not another pass@k estimator.
- **Length ratio**: first successful execution in fixed logical repetition order,
  separately for each problem and architecture. Divide generated proof-body lines
  by reference proof-body lines, then average over solved problems. Blank lines
  and comments are excluded. Do not choose the shortest successful proof.
- **Length groups**: sort 137 reference line counts ascending, tie-break by problem
  ID, and split into 46 Short, 46 Medium and 45 Long problems.
- **Completion rounds**: the completion round of the first successful execution;
  exclude never-solved problems. This is neither the minimum round across five
  executions nor the mean of all executions.
- **Resource averages**: all 685 executions per architecture, including failures.
  Elapsed time is wall-clock execution time, not a sum of concurrent lanes.
  Token/cost totals cover responses with recorded usage. Some responses have
  missing usage; the recomputed JSON reports these counts. Costs are recorded API
  figures and should not be treated as a complete billing ledger.

## 3. Current execution configuration

Use `configs/two_goal_experiment.env` with the runner in the setup guide.

| Setting | Value |
|---|---|
| Orchestrator | `openai/gpt-6-luna-pro` |
| Natural / Generator / Repair | `openai/gpt-6-luna` |
| Provider | `openai/flex`, no silent fallback |
| Worker reasoning | `none` |
| Planner reasoning | Model alias default |
| Rounds / trials | 3 rounds; 3 trials per lane per round |
| Model-call timeout, including Repair | 900 s |
| Lean verification timeout | 300 s |
| Output cap | 16,384 tokens; Repair 2,048 |
| Output recovery | At most 3 requests including the initial request |
| Transport recovery | At most 5 transport failures per node call |
| Lean | 4.27.0 |
| Mathlib | `a3a10db0e9d66acbebf76c5e6a135066525ac900` |
| miniF2F environment | `f0a20e14c1eeccd859d51bb4c2b3ee487889c303` |
| Tested Python | 3.13.3 |

Output-format/length recovery happens inside a model call and is separate from
the three proof trials. Equal round limits do not imply equal compute budgets:
Single has at most nine proof trials, Dual eighteen across both lanes.
The miniF2F checkout supplies the Lean environment, not evaluation problems.
Sampling temperature, top-p and model sampling seed are not fixed by this config.
Hosted models are stochastic, so new runs need not reproduce the archived counts.

## 4. Install and run

[CURRENT_SETUP.md](docs/CURRENT_SETUP.md) lists environment setup, offline checks,
plan generation, explicit paid execution and offline grading. Only
`data/book137_v1/problems.jsonl` is passed as the model-input dataset.
Do not pass labels, reference answers or archived results.

## 5. Build the manuscript

The submitted manuscript is `paper/paper.hwp`. The editable LaTeX conversion is
`paper/paper.tex`; its seven image assets are in
`paper/assets/`. Install XeLaTeX with `kotex`, Times New Roman, UnBatang and
Apple SD Gothic Neo, then run from `paper/`:

```bash
xelatex -interaction=nonstopmode -halt-on-error paper.tex
xelatex -interaction=nonstopmode -halt-on-error paper.tex
```

The supplied PDF is the checked build. On another OS, substitute available fonts
explicitly and inspect layout; line/page breaks can differ. Historical log files,
intermediate manuscripts and discarded analyses are not build inputs.
