from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .dual_runtime import DualOrchestrator
from .memory import JsonlTrialMemory
from .model import ModelClient, ModelClientError
from .proofnet import ProofNetExample, safe_example_id
from .verifier import LakeVerifier


@dataclass(frozen=True)
class DualEvaluationConfig:
    project_root: Path
    output_dir: Path
    max_trials: int = 3
    max_rounds: int = 3
    verify_timeout_seconds: int = 60
    verifier_module_name: str = "Scratch.Generated"
    verifier_lean_file: Path | None = None
    verifier_prelude: str | None = None
    strict_timeout_guard: bool = False


class DualProofNetEvaluator:
    def __init__(
        self,
        model_client: ModelClient,
        config: DualEvaluationConfig,
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
        example_id = safe_example_id(example.id)
        truth_memory_path = self.config.output_dir / f"{example_id}.truth_memory.jsonl"
        falsity_memory_path = self.config.output_dir / f"{example_id}.falsity_memory.jsonl"
        memory_path = self.config.output_dir / f"{example_id}.trial_memory.jsonl"
        truth_trials = (
            len(truth_memory_path.read_text(encoding="utf-8").splitlines())
            if truth_memory_path.exists()
            else 0
        )
        falsity_trials = (
            len(falsity_memory_path.read_text(encoding="utf-8").splitlines())
            if falsity_memory_path.exists()
            else 0
        )
        self.write_combined_memory(
            memory_path=memory_path,
            truth_memory_path=truth_memory_path,
            falsity_memory_path=falsity_memory_path,
        )
        return _without_empty_optionals({
            "id": example.id,
            "mode": "dual",
            "split": example.split,
            "final_status": "not_solved",
            "trials": truth_trials,
            "proof_trials": truth_trials,
            "falsity_opinions": falsity_trials,
            "rounds": 0,
            "allowed_imports": list(example.allowed_imports),
            "lean_header": example.header,
            "memory_path": str(memory_path),
            "rationale": str(exc),
            "false_evidence": "none",
            "dual_truth_trials": truth_trials,
            "dual_falsity_opinions": falsity_trials,
        })

    def evaluate_one(self, example: ProofNetExample) -> dict[str, Any]:
        example_id = safe_example_id(example.id)
        truth_memory_path = self.config.output_dir / f"{example_id}.truth_memory.jsonl"
        falsity_memory_path = self.config.output_dir / f"{example_id}.falsity_memory.jsonl"
        memory_path = self.config.output_dir / f"{example_id}.trial_memory.jsonl"
        truth_memory_path.write_text("", encoding="utf-8")
        falsity_memory_path.write_text("", encoding="utf-8")
        memory_path.write_text("", encoding="utf-8")
        orchestrator = DualOrchestrator(
            model_client=self.model_client,
            verifier=LakeVerifier(
                project_root=self.config.project_root,
                module_name=self.config.verifier_module_name,
                lean_file=self.config.verifier_lean_file,
                prelude=self.config.verifier_prelude or "",
                timeout_seconds=self.config.verify_timeout_seconds,
                strict_timeout_guard=self.config.strict_timeout_guard,
            ),
            truth_memory=JsonlTrialMemory(truth_memory_path, lane="truth"),
            falsity_memory=JsonlTrialMemory(falsity_memory_path, lane="falsity"),
            project_root=self.config.project_root,
            max_trials=self.config.max_trials,
            max_rounds=self.config.max_rounds,
        )
        run_result = orchestrator.run(
            example.problem_text,
            formal_statement=example.formal_statement,
            allowed_imports=example.allowed_imports,
            lean_header=example.header,
            expected_false_transform=example.false_transform,
            expected_false_fix=example.false_fix,
        )
        self.write_memory_records(memory_path, run_result.get("trial_memory", []))
        return _without_empty_optionals({
            "id": example.id,
            "mode": "dual",
            "local_audit_dir": run_result.get("local_audit_dir"),
            "split": example.split,
            "final_status": run_result.get(
                "final_status", self.final_status_from_decision(run_result["decision"])
            ),
            "trials": run_result["trials"],
            "proof_trials": run_result.get("proof_trials", run_result["dual_truth_trials"]),
            "falsity_opinions": run_result.get(
                "falsity_opinions", run_result["dual_falsity_opinions"]
            ),
            "rounds": run_result["rounds"],
            "disproof_trials": run_result.get("disproof_trials", 0),
            "goals": run_result.get("goals", {}),
            "validation_conflict": run_result.get("validation_conflict", False),
            "allowed_imports": list(example.allowed_imports),
            "lean_header": example.header,
            "memory_path": str(memory_path),
            "rationale": run_result["rationale"],
            "false_evidence": run_result.get("false_evidence", "none"),
            "correction_to_true": run_result.get("correction_to_true", ""),
            "false_transform": example.false_transform,
            "expected_false_fix": list(example.false_fix),
            "dual_truth_trials": run_result["dual_truth_trials"],
            "dual_falsity_opinions": run_result["dual_falsity_opinions"],
            "dual_falsity_trials": run_result.get(
                "dual_falsity_trials", run_result["dual_falsity_opinions"]
            ),
        })

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
    def final_status_from_decision(decision: str) -> str:
        if decision == "true":
            return "solved"
        if decision == "false":
            return "false"
        return "not_solved"

    @staticmethod
    def write_combined_memory(
        *,
        memory_path: Path,
        truth_memory_path: Path,
        falsity_memory_path: Path,
    ) -> None:
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        with memory_path.open("w", encoding="utf-8") as handle:
            for lane, path in (
                ("truth", truth_memory_path),
                ("falsity", falsity_memory_path),
            ):
                if not path.exists():
                    continue
                for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if not line.strip():
                        continue
                    payload = json.loads(line)
                    payload = {
                        "lane": lane,
                        "trial_index": index,
                        **payload,
                    }
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    @staticmethod
    def write_memory_records(
        memory_path: Path, records: list[dict[str, Any]]
    ) -> None:
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        with memory_path.open("w", encoding="utf-8") as handle:
            for record in records:
                payload = {
                    "lane": record.get("lane", ""),
                    "trial_index": record.get("trial_index", ""),
                    "informal": record.get("informal", ""),
                    "formal_state": record.get("formal_state", ""),
                    "lean_error_log": record.get("lean_error_log", ""),
                    "retry_plan": record.get("retry_plan", ""),
                }
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _without_empty_optionals(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if value not in ("", [], {}, ())
    }
