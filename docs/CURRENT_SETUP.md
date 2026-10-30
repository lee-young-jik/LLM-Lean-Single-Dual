# Install and run the 137-problem version

## Install

Use Python 3.11+ (tested: 3.13.3), Git and Elan. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-tested.txt
python -m pip install -e .
python scripts/validate_release.py
scripts/setup_minif2f.sh
```

The last command prepares the pinned Lean 4.27.0 environment and builds it. It
refuses to overwrite an existing checkout. It downloads environment dependencies,
not another evaluation dataset. See [exact versions](../REPRODUCIBILITY.md).

## Credentials

```bash
cp .env.example .env
```

Set `OPENROUTER_API_KEY` locally. Never commit `.env`. The current route does not
require Codex CLI OAuth. LangSmith is optional; local audits work without it.
Model/provider availability depends on the account. Report any changed route as
a new configuration rather than silently substituting a model.

## Offline checks

```bash
python scripts/check_two_goal_search.py --offline-only
python scripts/check_goal_lane_isolation.py
python scripts/check_flex_model_setup.py
python scripts/check_repair_context.py
python scripts/check_secrets.py
```

## Plan before paid execution

This command plans 1,370 complete executions without calling models:

```bash
python scripts/run_logged_experiment.py   --jsonl data/book137_v1/problems.jsonl --split book137_v1   --ids $(cat data/book137_v1/ids.txt)   --project-root .datasets/miniF2F-lean4   --config-env configs/two_goal_experiment.env   --repetitions 5 --memory-conditions full   --output-dir .runs/book137_plan
```

To run, use a new output directory and explicitly append `--run`. For a small
trial, supply one ID and `--repetitions 1`. Old output directories are not overwritten.
Each independent execution starts with empty memory. Single sees its prior-round
records; Dual sees both lanes' previous-round records but no current-round sibling
records. Within-round code repair uses Lean errors and Repair guidance.

Grade only after execution:

```bash
python scripts/analyze_logged_experiment.py .runs/YOUR_RUN   --labels data/book137_v1/labels.jsonl   --metadata data/book137_v1/problems.jsonl
```

Never use labels or reference answers as `--jsonl`. Fresh stochastic results are
in a separate output directory.
