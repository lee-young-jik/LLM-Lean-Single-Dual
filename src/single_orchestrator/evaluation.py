from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .memory import JsonlTrialMemory
from .model import ModelClient, ModelClientError
from .proofnet import ProofNetExample, safe_example_id
from .runtime import SingleOrchestrator
from .verifier import LakeVerifier


@dataclass(frozen=True)
class EvaluationConfig:
    project_root: Path
    output_dir: Path
    max_trials: int = 3
    max_strategy_rounds: int = 3
    verify_timeout_seconds: int = 60
    verifier_module_name: str = "Scratch.Generated"
    verifier_lean_file: Path | None = None
    verifier_prelude: str | None = None
    strict_timeout_guard: bool = False


class ProofNetEvaluator:
    def __init__(
        self,
        model_client: ModelClient,
        config: EvaluationConfig,
        judge_model_client: ModelClient | None = None,
    ):
        self.model_client = model_client
        self.judge_model_client = judge_model_client
        self.config = config

    def evaluate(self, examples: list[ProofNetExample], output_path: Path) -> list[dict[str, Any]]:
        self._validate_environment()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict[str, Any]] = []
        with output_path.open("w", encoding="utf-8") as handle:
            for example in examples:
                try:
                    result = self.evaluate_one(example)
                except ModelClientError as exc:
                    result = self.model_error_result(example, exc)
                results.append(result)
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                handle.flush()
        return results

    def model_error_result(self, example: ProofNetExample, exc: ModelClientError) -> dict[str, Any]:
        memory_path = self.config.output_dir / f"{safe_example_id(example.id)}.trial_memory.jsonl"
        trials = (
            len(
                [
                    line
                    for line in memory_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            )
            if memory_path.exists()
            else 0
        )
        return _without_empty_optionals({
            "id": example.id,
            "mode": "single",
            "split": example.split,
            "final_status": "not_solved",
            "trials": trials,
            "proof_trials": trials,
            "falsity_opinions": 0,
            "rounds": 0,
            "allowed_imports": list(example.allowed_imports),
            "lean_header": example.header,
            "memory_path": str(memory_path),
            "rationale": str(exc),
            "false_evidence": "none",
        })

    def evaluate_one(self, example: ProofNetExample) -> dict[str, Any]:
        memory_path = self.config.output_dir / f"{safe_example_id(example.id)}.trial_memory.jsonl"
        orchestrator = SingleOrchestrator(
            model_client=self.model_client,
            verifier=LakeVerifier(
                project_root=self.config.project_root,
                module_name=self.config.verifier_module_name,
                lean_file=self.config.verifier_lean_file,
                prelude=self.config.verifier_prelude or "",
                timeout_seconds=self.config.verify_timeout_seconds,
                strict_timeout_guard=self.config.strict_timeout_guard,
            ),
            memory=JsonlTrialMemory(memory_path),
            project_root=self.config.project_root,
            max_trials=self.config.max_trials,
            max_strategy_rounds=self.config.max_strategy_rounds,
        )
        run_result = orchestrator.run(
            example.problem_text,
            formal_statement=example.formal_statement,
            allowed_imports=example.allowed_imports,
            lean_header=example.header,
            expected_false_transform=example.false_transform,
            expected_false_fix=example.false_fix,
        )
        result: dict[str, Any] = _without_empty_optionals({
            "id": example.id,
            "mode": "single",
            "local_audit_dir": run_result.get("local_audit_dir"),
            "split": example.split,
            "final_status": run_result.get("final_status", self.final_status(run_result)),
            "trials": run_result["trials"],
            "proof_trials": run_result.get("proof_trials", run_result["trials"]),
            "falsity_opinions": run_result.get("falsity_opinions", 0),
            "rounds": run_result["rounds"],
            "disproof_trials": run_result.get("disproof_trials", 0),
            "goals": run_result.get("goals", {}),
            "validation_conflict": run_result.get("validation_conflict", False),
            "allowed_imports": list(example.allowed_imports),
            "lean_header": example.header,
            "memory_path": str(memory_path),
            "rationale": self.rationale(run_result),
            "false_evidence": run_result.get("false_evidence", "none"),
            "correction_to_true": run_result.get("correction_to_true", ""),
            "false_transform": example.false_transform,
            "expected_false_fix": list(example.false_fix),
        })
        return result

    def _validate_environment(self) -> None:
        prelude = self.config.verifier_prelude or ""
        if "MiniF2F.ProblemImports" not in prelude:
            return
        expected = self.config.project_root / "MiniF2F" / "ProblemImports.lean"
        if expected.exists():
            return
        raise ValueError(
            "MiniF2F fixed prelude requires project_root to be the miniF2F Lean "
            f"project root containing MiniF2F/ProblemImports.lean. Got "
            f"{self.config.project_root}; expected file is missing: {expected}"
        )

    @staticmethod
    def final_status(run_result: dict[str, Any]) -> str:
        if run_result.get("final_status") in {"solved", "not_solved", "false"}:
            return str(run_result["final_status"])
        if not run_result.get("success", False):
            return "not_solved"
        return "solved"

    @staticmethod
    def rationale(run_result: dict[str, Any]) -> str:
        final_status = ProofNetEvaluator.final_status(run_result)
        if final_status == "solved":
            return "Verified proof accepted within budget."
        if final_status == "false":
            return run_result.get("rationale", "False decision accepted.")
        if run_result.get("rationale"):
            return run_result["rationale"]
        return "Budget exhausted without a verified accepted proof."


def _without_empty_optionals(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if value not in ("", [], {}, ())
    }
