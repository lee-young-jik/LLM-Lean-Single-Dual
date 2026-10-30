from __future__ import annotations

import argparse
from pathlib import Path

from .evaluation import EvaluationConfig, ProofNetEvaluator
from .model import make_prompt_routing_model_client
from .proofnet import (
    attach_hidden_false_keys,
    filter_examples,
    load_proofnet_jsonl,
    load_proofnet_split,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate on DeepSeek ProofNet Lean 4 data.")
    parser.add_argument(
        "--dataset-root",
        type=Path,
        help="Path to a local DeepSeek-Prover-V1.5 checkout.",
    )
    parser.add_argument(
        "--jsonl",
        type=Path,
        help="Direct path to DeepSeek datasets/proofnet.jsonl.",
    )
    parser.add_argument(
        "--labels-jsonl",
        type=Path,
        help="Private labels containing false_transform and expected_false_fix.",
    )
    parser.add_argument("--split", default="valid", help="Split name to load from JSONL.")
    parser.add_argument("--ids", nargs="*", help="Optional DeepSeek ProofNet example names.")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, default=Path(".runs/proofnet"))
    parser.add_argument("--output", type=Path, default=Path(".runs/proofnet/results.jsonl"))
    parser.add_argument("--model", help="Codex model for generation agents.")
    parser.add_argument("--orchestrator-model", help="Model for orchestrator strategy prompts.")
    parser.add_argument("--false-judgment-model", help="Model for preliminary false judgment.")
    parser.add_argument("--nature-model", help="Model for Nature agent prompts.")
    parser.add_argument("--lean-model", help="Model for Lean generator prompts.")
    parser.add_argument("--retry-model", help="Model for retry planner prompts.")
    parser.add_argument(
        "--strict-model-routing",
        action="store_true",
        help="Fail if any prompt has no explicit model route.",
    )
    parser.add_argument("--judge-model", help="Deprecated; semantic judge is disabled.")
    parser.add_argument("--model-timeout", type=int, default=300, help="Seconds per model call; 0 disables the timeout.")
    parser.add_argument("--judge-timeout", type=int)
    parser.add_argument("--max-trials", type=int, default=3)
    parser.add_argument("--max-strategy-rounds", type=int, default=3)
    parser.add_argument("--verify-timeout", type=int, default=60, help="Seconds per Lean build; 0 disables the timeout.")
    parser.add_argument(
        "--strict-timeout-guard",
        action="store_true",
        help=(
            "Reject timeout-sensitive tactics such as linarith/nlinarith "
            "before running Lean."
        ),
    )
    parser.add_argument("--verifier-module", default="Scratch.Generated")
    parser.add_argument("--verifier-file", type=Path)
    parser.add_argument("--verifier-prelude", default="import Mathlib.Tactic")
    args = parser.parse_args(argv)

    examples = _load_examples(args)
    examples = filter_examples(
        examples,
        start=args.start,
        limit=args.limit,
        ids=set(args.ids) if args.ids else None,
    )

    project_root = args.project_root.resolve()
    output_dir = _resolve_under(project_root, args.output_dir)
    output_path = _resolve_under(project_root, args.output)
    model_client = make_prompt_routing_model_client(
        project_root,
        default_model=args.model,
        timeout_seconds=args.model_timeout,
        prompt_models=_prompt_models(args),
        strict=args.strict_model_routing,
    )
    evaluator = ProofNetEvaluator(
        model_client=model_client,
        config=EvaluationConfig(
            project_root=project_root,
            output_dir=output_dir,
            max_trials=args.max_trials,
            max_strategy_rounds=args.max_strategy_rounds,
            verify_timeout_seconds=args.verify_timeout,
            verifier_module_name=args.verifier_module,
            verifier_lean_file=args.verifier_file,
            verifier_prelude=args.verifier_prelude,
            strict_timeout_guard=args.strict_timeout_guard,
        ),
    )
    from .local_audit import safe
    import json, hashlib
    output_dir.mkdir(parents=True, exist_ok=True)
    configuration = {'arguments': vars(args), 'selected_ids': [x.id for x in examples],
                     'dataset_sha256': hashlib.sha256(args.jsonl.read_bytes()).hexdigest() if args.jsonl else None}
    from uuid import uuid4
    text = json.dumps(safe(configuration), default=str, ensure_ascii=False, indent=2)+'\n'
    (output_dir/('invocation_'+uuid4().hex+'.json')).write_text(text, encoding='utf-8')
    (output_dir/'invocation.json').write_text(text, encoding='utf-8')
    evaluator.evaluate(examples, output_path)
    print(f"Wrote {len(examples)} ProofNet result(s) to {output_path}")
    return 0


def _load_examples(args: argparse.Namespace):
    if args.jsonl:
        examples = load_proofnet_jsonl(args.jsonl, split=args.split)
        if args.labels_jsonl:
            examples = attach_hidden_false_keys(examples, args.labels_jsonl)
        return examples
    if not args.dataset_root:
        raise SystemExit("Provide --dataset-root or --jsonl.")
    return load_proofnet_split(args.dataset_root, args.split)


def _resolve_under(project_root: Path, path: Path) -> Path:
    return path if path.is_absolute() else project_root / path


def _prompt_models(args: argparse.Namespace) -> dict[str, str]:
    prompt_models: dict[str, str] = {}
    if args.orchestrator_model:
        prompt_models["orchestrator.strategy"] = args.orchestrator_model
        prompt_models["orchestrator.restrategy"] = args.orchestrator_model
        prompt_models["orchestrator.false_decision"] = args.orchestrator_model
        prompt_models["orchestrator.false_judgment_review"] = args.orchestrator_model
        prompt_models["correction_judge.compare"] = args.orchestrator_model
        prompt_models["false_check"] = args.orchestrator_model
    if args.false_judgment_model:
        prompt_models["false_judgment_agent.assess"] = args.false_judgment_model
        prompt_models["false_reason"] = args.false_judgment_model
    if args.nature_model:
        prompt_models["nature_agent.initial"] = args.nature_model
        prompt_models["nature_agent.retry"] = args.nature_model
    if args.lean_model:
        prompt_models["lean_generator.generate"] = args.lean_model
    if args.retry_model:
        prompt_models["retry_planner.plan"] = args.retry_model
    return prompt_models


if __name__ == "__main__":
    raise SystemExit(main())
