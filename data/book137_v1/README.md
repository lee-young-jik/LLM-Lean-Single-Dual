# Final 137-problem analysis dataset

논문의 최종 분석 대상 **137문제(Prove 62개, Disprove 75개)**입니다.
공개 입력과 평가용 정답을 분리했습니다. 코드와 프롬프트는 저장소의
`src/single_orchestrator/`에 있으며 실행 환경은
[CURRENT_SETUP.md](../../docs/CURRENT_SETUP.md)에 설명되어 있습니다.

## Files and model-input boundary

- `problems.jsonl`: natural-language statements, formal Prove statements, Lean preludes and source identifiers; split `book137_v1`. The runtime constructs the corresponding Disprove goal. **Only this file is a model-input dataset.**
- `labels.jsonl`: evaluator-only Prove/Disprove labels and reference-length metadata.
- `reference_answers.jsonl`: evaluator-only reference proof bodies and complete Lean certificates.
- `ids.txt`: the 137 problem IDs.
- `manifest.json`: counts and SHA-256 hashes.

Do not send labels, reference answers to the agents.
Book source identifiers and original statements are retained. Sources:
[Hammack](https://richardhammack.github.io/BookOfProof/),
[Lebl](https://jirka.org/ra/), [Axler](https://linear.axler.net/),
[Erdman](https://web.pdx.edu/~erdman/CALCULUS/CALCULUS_pdf.pdf), and
[Judson](https://judsonbooks.org/aata-files/aata-html/aata-toc.html).
This export does not change source licensing or redistribute textbooks.

## Prompts and settings

- [Actual two-goal prompts](../../src/single_orchestrator/goal_prompts.py)
- [Goal selection, memory and execution flow](../../src/single_orchestrator/goal_search.py)
- [Model/provider/budget configuration](../../configs/two_goal_experiment.env)
- [Role and memory contracts](../../docs/FINAL_PIPELINE.md)
- [Installation and pinned Lean/Mathlib environment](../../docs/CURRENT_SETUP.md)

Single selects a goal each round; Dual retains both goals. Both use at most
3 rounds and 3 Lean attempts per lane per round. Pass@5 means five independent
complete executions, with memory reset between executions. 

## Offline validation and plan

```bash
python scripts/validate_release.py
```

After installing the dependencies in the setup guide, this command writes a plan
for all 137 problems, both architectures and five repetitions (1,370 executions),
**without making model calls**:

```bash
python scripts/run_logged_experiment.py \
  --jsonl data/book137_v1/problems.jsonl --split book137_v1 \
  --ids $(cat data/book137_v1/ids.txt) \
  --project-root .datasets/miniF2F-lean4 \
  --config-env configs/two_goal_experiment.env \
  --repetitions 5 --memory-conditions full \
  --output-dir .runs/book137_plan
```

To run a fresh paid experiment, explicitly append `--run` and use a new output
directory. A new stochastic run is not expected to reproduce the manuscript's
historical outcomes exactly. Grade offline with `scripts/analyze_logged_experiment.py`
using this directory's `labels.jsonl` and `problems.jsonl` as metadata.
