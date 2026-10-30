# Final manuscript tables

These files reproduce the final manuscript's reported analysis without new model calls.
They are evaluator-only and must not be sent to agents.

- `executions.jsonl`: 137 problems × 2 architectures × 5 logical repetitions;
  outcomes, successful proof bodies, recorded usage and elapsed times.
- `single_choices.jsonl`: Single's round-by-round selected direction.
- `manifest.json`: Record counts and SHA-256 hashes.
- `tables.json`: recomputed final tables and the paired Pass@5 comparison.

Run `python3 scripts/reproduce_paper_tables.py --check` from the repository root.
See [definitions and environment](../../REPRODUCIBILITY.md).

A successful proof body's presence records an earlier Lean-verified outcome; this
export and the offline aggregation script do not perform new Lean certification.
Original full audit logs are not included in this minimal package.
