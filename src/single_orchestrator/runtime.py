from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

from .lean_context import retrieve_lean_context
from .lean_code import complete_lean_source, lean_generation_rejection_reason
from .judge import LLMJudge
from .false_review import FalseReviewRuntime
from .round_false_review import RoundFalseReviewRuntime
from .memory import JsonlTrialMemory
from .model import ModelClient, ModelClientError, make_default_model_client
from .prompts import BuiltPrompt, PromptRegistry
from .state import GraphState, JudgeContext, TrialMemoryRecord
from .trace import traceable
from .verifier import LakeVerifier


@dataclass
class SingleOrchestratorRuntime:
    model_client: ModelClient
    prompts: PromptRegistry
    verifier: LakeVerifier
    memory: JsonlTrialMemory
    project_root: Path = field(default_factory=Path.cwd)
    judge: LLMJudge | None = None
    judge_context: JudgeContext | None = None
    max_trials: int = 3
    max_strategy_rounds: int = 3

    def create_strategy(self, state: GraphState) -> dict[str, Any]:
        if state.get("strategy"):
            prompt = self.prompts.build(
                "orchestrator.restrategy",
                problem=state["problem"],
                lean_header=state.get("lean_header", ""),
                strategy=state.get("strategy", ""),
                trial_memory=self._orchestrator_trial_memory_list(
                    state.get("trial_memory", [])
                ),
            )
            strategy_round = state.get("strategy_round", 1) + 1
        else:
            prompt = self.prompts.build(
                "orchestrator.strategy",
                problem=state["problem"],
                lean_header=state.get("lean_header", ""),
            )
            strategy_round = 1

        strategy = self.run_round_orchestrator(
            prompt.name,
            prompt.version,
            prompt.inputs,
            prompt.text,
            strategy_round=strategy_round,
            langsmith_extra=self._trace_extra(
                name=f"round {strategy_round}",
                strategy_round=strategy_round,
                phase="single_round",
            ),
        )
        return {
            "strategy": strategy,
            "strategy_round": strategy_round,
            "trial_index": 1,
            "needs_restrategy": False,
            "needs_false_decision": False,
            "current_informal": "",
            "current_lean_code": "",
            "current_retry_plan": "",
            "model_error_log": "",
        }

    @traceable(name="single_round", run_type="chain")
    def run_round_orchestrator(
        self,
        prompt_name: str,
        prompt_version: str,
        prompt_inputs: dict[str, Any],
        prompt_text: str,
        strategy_round: int,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self._complete_orchestrator_prompt(
            prompt_name,
            prompt_version,
            prompt_inputs,
            prompt_text,
            langsmith_extra=self._trace_extra(
                name=f"{strategy_round} {prompt_name}",
                strategy_round=strategy_round,
                phase=prompt_name,
            ),
        )

    def create_nature_solution(self, state: GraphState) -> dict[str, str]:
        return self.run_nature_agent_node(
            state,
            langsmith_extra=self._trace_extra(
                name=f"{self._trial_label(state)} nature_agent",
                strategy_round=state.get("strategy_round", 1),
                trial_index=state.get("trial_index", 1),
                trial_label=self._trial_label(state),
                phase="nature_agent",
            ),
        )

    @traceable(name="nature_agent", run_type="chain")
    def run_nature_agent_node(
        self,
        state: GraphState,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        if state.get("trial_memory"):
            previous_trial = self._compact_trial_feedback(state["trial_memory"][-1])
            prompt = self.prompts.build(
                "nature_agent.retry",
                problem=state["problem"],
                strategy=state.get("strategy", ""),
                previous_trial=previous_trial,
            )
        else:
            prompt = self.prompts.build(
                "nature_agent.initial",
                problem=state["problem"],
                strategy=state.get("strategy", ""),
            )
        return {
            "current_informal": self._complete_nature_prompt(
                prompt.name,
                prompt.version,
                prompt.inputs,
                prompt.text,
                langsmith_extra=self._trace_extra(
                    name=f"{self._trial_label(state)} nature_agent.informal",
                    strategy_round=state.get("strategy_round", 1),
                    trial_index=state.get("trial_index", 1),
                    trial_label=self._trial_label(state),
                    phase=prompt.name,
                ),
            )
        }

    def create_informal(self, state: GraphState) -> dict[str, str]:
        return self.create_nature_solution(state)

    def generate_lean(self, state: GraphState) -> dict[str, str]:
        return self.run_lean_generator_node(
            state,
            langsmith_extra=self._trace_extra(
                name=f"{self._trial_label(state)} lean_generator",
                strategy_round=state.get("strategy_round", 1),
                trial_index=state.get("trial_index", 1),
                trial_label=self._trial_label(state),
                phase="lean_generator",
            ),
        )

    @traceable(name="lean_generator", run_type="chain")
    def run_lean_generator_node(
        self,
        state: GraphState,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        previous_trial = (
            self._compact_trial_feedback(state["trial_memory"][-5:])
            if state.get("trial_memory")
            else {}
        )
        lean_context = (
            self._fixed_minif2f_lean_context()
            if self._uses_fixed_minif2f_prelude()
            else retrieve_lean_context(
                self.project_root,
                formal_statement=state.get("formal_statement", ""),
                informal=state.get("current_informal", ""),
                previous_feedback=previous_trial,
            )
        )
        prompt = self.prompts.build(
            "lean_generator.generate",
            problem=state["problem"],
            current_informal=state.get("current_informal", ""),
            formal_statement=state.get("formal_statement", ""),
            allowed_imports=state.get("allowed_imports", []),
            lean_header=state.get("lean_header", ""),
            lean_context=lean_context,
            previous_trial=previous_trial,
        )
        try:
            raw = self._complete_lean_prompt(
                prompt.name,
                prompt.version,
                prompt.inputs,
                prompt.text,
                langsmith_extra=self._trace_extra(
                    name=f"{self._trial_label(state)} lean_generator.lean_code",
                    strategy_round=state.get("strategy_round", 1),
                    trial_index=state.get("trial_index", 1),
                    trial_label=self._trial_label(state),
                    phase=prompt.name,
                ),
            )
            lean_code = complete_lean_source(
                raw,
                state.get("formal_statement", ""),
                preserve_header=not self._uses_fixed_minif2f_prelude(),
            )
            rejection_reason = ""
            if not lean_code.strip():
                rejection_reason = lean_generation_rejection_reason(raw)
            return {
                "current_lean_code": lean_code,
                "model_error_log": rejection_reason,
            }
        except ModelClientError as exc:
            return {
                "current_lean_code": "",
                "model_error_log": f"Lean generation model failed: {exc}",
            }

    def verify_lean(self, state: GraphState) -> dict[str, Any]:
        return self.run_verifier_node(
            state,
            langsmith_extra=self._trace_extra(
                name=f"{self._trial_label(state)} verifier",
                strategy_round=state.get("strategy_round", 1),
                trial_index=state.get("trial_index", 1),
                trial_label=self._trial_label(state),
                phase="verifier",
            ),
        )

    @traceable(name="verifier", run_type="chain")
    def run_verifier_node(
        self,
        state: GraphState,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if state.get("model_error_log"):
            return {
                "verifier_result": {
                    "success": False,
                    "formal_state": "",
                    "lean_error_log": state["model_error_log"],
                    "returncode": None,
                    "command": [],
                    "timed_out": "timed out" in state["model_error_log"],
                    "has_prohibited_placeholder": False,
                }
            }
        verifier_result = self.verifier.verify(
            state.get("current_lean_code", ""),
            formal_statement=state.get("formal_statement", ""),
        )
        return {"verifier_result": self._with_compact_lean_error_log(verifier_result)}

    def judge_semantics(self, state: GraphState) -> dict[str, Any]:
        if self.judge is None or self.judge_context is None:
            return {}

        verifier_result = state["verifier_result"]
        judge_result = self.judge.judge_context(
            self.judge_context, verifier_result["formal_state"]
        ).to_json()
        return {"judge_result": judge_result}

    def create_retry_plan(self, state: GraphState) -> dict[str, str]:
        return self.run_retry_planner_node(
            state,
            langsmith_extra=self._trace_extra(
                name=f"{self._trial_label(state)} retry_planner",
                strategy_round=state.get("strategy_round", 1),
                trial_index=state.get("trial_index", 1),
                trial_label=self._trial_label(state),
                phase="retry_planner",
            ),
        )

    @traceable(name="retry_planner", run_type="chain")
    def run_retry_planner_node(
        self,
        state: GraphState,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        verifier_result = state["verifier_result"]
        prompt = self.prompts.build(
            "retry_planner.plan",
            problem=state["problem"],
            failure_context=self._compact_failure_context(state),
        )
        try:
            retry_plan = self._complete_retry_prompt(
                prompt.name,
                prompt.version,
                prompt.inputs,
                prompt.text,
                langsmith_extra=self._trace_extra(
                    name=f"{self._trial_label(state)} retry_planner.retry_plan",
                    strategy_round=state.get("strategy_round", 1),
                    trial_index=state.get("trial_index", 1),
                    trial_label=self._trial_label(state),
                    phase=prompt.name,
                ),
            )
        except ModelClientError as exc:
            retry_plan = self._fallback_retry_plan(str(exc))
        return {
            "current_retry_plan": retry_plan
        }

    def record_trial(self, state: GraphState) -> dict[str, Any]:
        verifier_result = state["verifier_result"]
        record: TrialMemoryRecord = {
            "informal": state.get("current_informal", ""),
            "lean_code": state.get("current_lean_code", ""),
            "verifier_result": dict(verifier_result),
            "judge_result": dict(state.get("judge_result", {})),
            "trial_index": state.get("trial_index", 1),
            "strategy_round": state.get("strategy_round", 1),
            "formal_state": verifier_result["formal_state"],
            "lean_error_log": self._trial_error_log(state),
            "retry_plan": state.get("current_retry_plan", ""),
        }
        self.memory.append(record)
        trial_memory = [*state.get("trial_memory", []), record]

        if self._is_success(state):
            return {
                "trial_memory": trial_memory,
                "done": True,
                "needs_restrategy": False,
                "needs_false_decision": False,
                "final_status": "solved",
            }

        trial_index = state.get("trial_index", 1)
        if trial_index >= self.max_trials:
            return {
                "trial_memory": trial_memory,
                "done": False,
                "needs_restrategy": False,
                "needs_false_decision": True,
                "current_informal": "",
                "current_lean_code": "",
                "current_retry_plan": "",
                "model_error_log": "",
            }

        return {
            "trial_memory": trial_memory,
            "trial_index": trial_index + 1,
            "done": False,
            "needs_restrategy": False,
            "needs_false_decision": False,
            "current_informal": "",
            "current_lean_code": "",
            "current_retry_plan": "",
            "model_error_log": "",
            "judge_result": {},
        }

    def decide_false_after_round(self, state: GraphState) -> dict[str, Any]:
        evidence_memory = state.get("trial_memory", [])[-self.max_trials :]
        review = RoundFalseReviewRuntime(
            self.model_client, self.verifier,
            audit_path=self.memory.path.with_name("refutation_audit.jsonl"),
        ).review(
            formal_statement=state.get("formal_statement", ""),
            memory=evidence_memory,
            strategy_round=state.get("strategy_round", 1),
        )
        success = review.get("decision") == "false"
        decision = {
            "decision": "false" if success else "continue",
            # Failed refutation contents are audit-only, never search feedback.
            "rationale": str(review.get("why", "")) if success else "",
            "false_evidence": "lean_verified_negation" if success else "none",
            "correction_to_true": "",
        }

        if decision["decision"] == "false":
            return {
                "done": True,
                "needs_restrategy": False,
                "needs_false_decision": False,
                "final_status": "false",
                "false_evidence": decision["false_evidence"],
                "false_rationale": decision["rationale"],
                "correction_to_true": decision.get("correction_to_true", ""),
            }

        strategy_round = state.get("strategy_round", 1)
        if strategy_round >= self.max_strategy_rounds:
            return {
                "done": True,
                "needs_restrategy": False,
                "needs_false_decision": False,
                "final_status": "not_solved",
                "false_evidence": decision["false_evidence"],
                "false_rationale": decision["rationale"],
                "correction_to_true": decision.get("correction_to_true", ""),
                "final_restrategy": state.get("strategy", ""),
            }

        return {
            "done": False,
            "needs_restrategy": True,
            "needs_false_decision": False,
            "false_evidence": decision["false_evidence"],
            "false_rationale": decision["rationale"],
            "correction_to_true": decision.get("correction_to_true", ""),
            "current_informal": "",
            "current_lean_code": "",
            "current_retry_plan": "",
            "model_error_log": "",
            "judge_result": {},
        }

    def _false_agent_assessment(
        self,
        *,
        problem: str,
        formal_statement: str,
        evidence_memory: list[dict[str, str]],
        strategy_round: int,
    ) -> dict[str, str]:
        prompt = self.prompts.build(
            "false_judgment_agent.assess",
            problem=problem,
            formal_statement=formal_statement,
            evidence_memory=evidence_memory,
        )
        try:
            raw = self._complete_false_judgment_prompt(
                prompt.name,
                prompt.version,
                prompt.inputs,
                prompt.text,
                langsmith_extra=self._trace_extra(
                    name=f"{strategy_round} {prompt.name}",
                    strategy_round=strategy_round,
                    phase=prompt.name,
                ),
            )
        except ModelClientError as exc:
            return {
                "decision": "continue",
                "rationale": f"False judgment agent failed; continue conservatively. {exc}",
                "false_evidence": "none",
                "correction_to_true": "",
            }
        try:
            parsed = json.loads(self._strip_json_fence(raw))
        except json.JSONDecodeError:
            return {
                "decision": "continue",
                "rationale": raw,
                "false_evidence": "none",
                "correction_to_true": "",
            }
        correction_to_true = str(parsed.get("correction_to_true", "") or "").strip()
        false_evidence = str(parsed.get("false_evidence", "none") or "none")
        rationale = str(parsed.get("rationale", "") or "").strip()
        hypotheses_check = str(parsed.get("hypotheses_check", "") or "").strip()
        conclusion_check = str(parsed.get("conclusion_check", "") or "").strip()
        raw_uncertainties = parsed.get("uncertainties", [])
        uncertainties = (
            [str(item).strip() for item in raw_uncertainties if str(item).strip()]
            if isinstance(raw_uncertainties, list)
            else [str(raw_uncertainties).strip()]
            if str(raw_uncertainties).strip()
            else []
        )
        decision = str(parsed.get("decision", "continue") or "continue")
        if decision not in {"false", "continue", "unknown"}:
            decision = "continue"
        if (
            decision != "false"
            or not correction_to_true
            or false_evidence == "none"
            or not hypotheses_check
            or not conclusion_check
            or uncertainties
        ):
            return {
                "decision": "continue",
                "rationale": rationale or "False judgment agent did not identify a supported correction.",
                "false_evidence": false_evidence,
                "correction_to_true": correction_to_true,
                "hypotheses_check": hypotheses_check,
                "conclusion_check": conclusion_check,
                "uncertainties": uncertainties,
            }
        return {
            "decision": "false",
            "rationale": rationale,
            "false_evidence": false_evidence,
            "correction_to_true": correction_to_true,
            "hypotheses_check": hypotheses_check,
            "conclusion_check": conclusion_check,
            "uncertainties": uncertainties,
        }

    def _review_false_agent_result(
        self,
        *,
        problem: str,
        formal_statement: str,
        evidence_memory: list[dict[str, str]],
        false_agent_result: dict[str, str],
        expected_false_fix: list[str] | tuple[str, ...],
        strategy_round: int,
        lean_header: str = "",
        strategy: str = "",
    ) -> dict[str, str]:
        prompt = self.prompts.build(
            "orchestrator.false_decision",
            problem=problem,
            formal_statement=formal_statement,
            lean_header=lean_header,
            strategy=strategy,
            trial_memory={
                "trials": evidence_memory,
                "preliminary_false_judgment": false_agent_result,
            },
        )
        try:
            raw = self._complete_orchestrator_prompt(
                prompt.name,
                prompt.version,
                prompt.inputs,
                prompt.text,
                langsmith_extra=self._trace_extra(
                    name=f"{strategy_round} {prompt.name}",
                    strategy_round=strategy_round,
                    phase=prompt.name,
                ),
            )
        except ModelClientError as exc:
            return {
                "decision": "continue",
                "rationale": f"False judgment review failed; continue conservatively. {exc}",
                "false_evidence": "none",
                "correction_to_true": false_agent_result.get("correction_to_true", ""),
            }
        decision = self._parse_false_decision(raw)
        return self._apply_expected_false_fix_gate(
            decision,
            formal_statement=formal_statement,
            expected_false_fix=expected_false_fix,
            evidence_memory=evidence_memory,
        )

    @classmethod
    def _parse_false_decision(cls, raw: str) -> dict[str, str]:
        try:
            parsed = json.loads(cls._strip_json_fence(raw))
        except json.JSONDecodeError:
            return {
                "decision": "continue",
                "rationale": raw,
                "false_evidence": "none",
            }
        decision = parsed.get("decision", "continue")
        if decision not in {"false", "continue", "unknown"}:
            decision = "continue"
        false_evidence = str(parsed.get("false_evidence", "none") or "none")
        rationale = str(parsed.get("rationale", "")).strip()
        correction_to_true = str(parsed.get("correction_to_true", "") or "").strip()
        raw_uncertainties = parsed.get("uncertainties", [])
        uncertainties = (
            [str(item).strip() for item in raw_uncertainties if str(item).strip()]
            if isinstance(raw_uncertainties, list)
            else [str(raw_uncertainties).strip()]
            if str(raw_uncertainties).strip()
            else []
        )
        validation_complete = parsed.get("validation_complete") is True
        hypotheses_verified = parsed.get("hypotheses_verified") is True
        conclusion_falsified = parsed.get("conclusion_falsified") is True
        if decision == "false" and false_evidence not in {
            "validated_counterexample",
            "validated_negation",
            "clear_counterexample",
            "clear_negation",
            "explicit_contradiction",
            "wrong_constant",
            "relation_flip",
            "strictness_error",
            "missing_hypothesis",
        }:
            return {
                "decision": "continue",
                "rationale": (
                    "False decision was downgraded to continue because it did not "
                    "provide clear false evidence. "
                    f"Original rationale: {rationale}"
                ),
                "false_evidence": false_evidence,
                "correction_to_true": correction_to_true,
            }
        if decision == "false" and not rationale:
            return {
                "decision": "continue",
                "rationale": (
                    "False decision was downgraded to continue because it did not "
                    "clearly explain why the exact statement is false."
                ),
                "false_evidence": false_evidence,
                "correction_to_true": correction_to_true,
            }
        if decision == "false" and not correction_to_true:
            return {
                "decision": "continue",
                "rationale": (
                    "False decision was downgraded to continue because it did not "
                    "state what change would make the exact target true."
                ),
                "false_evidence": false_evidence,
                "correction_to_true": "",
            }
        return {
            "decision": decision,
            "rationale": rationale,
            "false_evidence": false_evidence,
            "correction_to_true": correction_to_true,
            "validation_complete": validation_complete,
            "hypotheses_verified": hypotheses_verified,
            "conclusion_falsified": conclusion_falsified,
            "uncertainties": uncertainties,
        }

    def _apply_expected_false_fix_gate(
        self,
        decision: dict[str, str],
        formal_statement: str,
        expected_false_fix: list[str] | tuple[str, ...],
        evidence_memory: list[dict[str, str]] | str,
    ) -> dict[str, str]:
        if decision.get("decision") != "false" or not expected_false_fix:
            return decision
        uncertainties = decision.get("uncertainties", [])
        if (
            decision.get("validation_complete") is not True
            or decision.get("hypotheses_verified") is not True
            or decision.get("conclusion_falsified") is not True
            or not isinstance(uncertainties, list)
            or bool(uncertainties)
        ):
            return {
                **decision,
                "decision": "continue",
                "rationale": (
                    "False decision was downgraded to continue because strict validation "
                    "was incomplete or retained uncertainty. "
                    f"Original rationale: {decision.get('rationale', '')}"
                ),
            }
        correction = decision.get("correction_to_true", "")
        judge = self._judge_correction_to_true(
            formal_statement=formal_statement,
            evidence_memory=evidence_memory,
            false_rationale=decision.get("rationale", ""),
            expected_false_fix=expected_false_fix,
            correction_to_true=correction,
        )
        if (
            judge.get("equivalent")
            and judge.get("evidence_supported")
            and not judge.get("uncertainties")
        ):
            return {
                **decision,
                "correction_judge_rationale": str(judge.get("rationale", "")),
            }
        expected = "; ".join(expected_false_fix)
        return {
            "decision": "continue",
            "rationale": (
                "False decision was downgraded to continue because the correction judge "
                "did not accept the proposed correction_to_true. "
                f"Expected correction: {expected}. Proposed correction: {correction}. "
                f"Judge rationale: {judge.get('rationale', '')}. "
                f"Original rationale: {decision.get('rationale', '')}"
            ),
            "false_evidence": decision.get("false_evidence", "none"),
            "correction_to_true": correction,
        }

    def _judge_correction_to_true(
        self,
        *,
        formal_statement: str,
        evidence_memory: list[dict[str, str]] | str,
        false_rationale: str,
        expected_false_fix: list[str] | tuple[str, ...],
        correction_to_true: str,
    ) -> dict[str, Any]:
        prompt = self.prompts.build(
            "correction_judge.compare",
            formal_statement=formal_statement,
            evidence_memory=evidence_memory,
            false_rationale=false_rationale,
            expected_correction="\n".join(expected_false_fix),
            proposed_correction=correction_to_true,
        )
        try:
            raw = self._complete_correction_judge_prompt(
                prompt.name,
                prompt.version,
                prompt.inputs,
                prompt.text,
            )
        except ModelClientError as exc:
            return {
                "equivalent": False,
                "rationale": f"Correction judge failed; continue conservatively. {exc}",
            }
        try:
            parsed = json.loads(self._strip_json_fence(raw))
        except json.JSONDecodeError:
            return {"equivalent": False, "rationale": raw}
        return {
            "equivalent": bool(parsed.get("equivalent", False)),
            "evidence_supported": bool(parsed.get("evidence_supported", False)),
            "uncertainties": (
                [str(item).strip() for item in parsed.get("uncertainties", []) if str(item).strip()]
                if isinstance(parsed.get("uncertainties", []), list)
                else [str(parsed.get("uncertainties", "")).strip()]
                if str(parsed.get("uncertainties", "")).strip()
                else []
            ),
            "rationale": str(parsed.get("rationale", "")).strip(),
        }

    @traceable(name="correction_judge.compare", run_type="llm")
    def _complete_correction_judge_prompt(
        self,
        prompt_name: str,
        prompt_version: str,
        prompt_inputs: dict[str, Any],
        prompt_text: str,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self._complete_prompt(prompt_name, prompt_version, prompt_inputs, prompt_text)

    @staticmethod
    def _strip_json_fence(raw: str) -> str:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return text

    def _failure_reason(self, state: GraphState) -> str:
        verifier_result = state["verifier_result"]
        if not verifier_result["success"]:
            error_log = verifier_result.get("lean_error_log", "")
            if verifier_result.get("timed_out") and "Lean generation model failed" in error_log:
                return "Lean generation model timed out before producing Lean code."
            if verifier_result.get("timed_out"):
                return "Lake build timed out."
            if "imports broad module" in error_log:
                return "Generated Lean used an overly broad import."
            if "imports unavailable module" in error_log:
                return "Generated Lean used an unavailable import."
            if verifier_result.get("has_prohibited_placeholder"):
                return "Generated Lean used a prohibited placeholder."
            return "Lean build failed."
        return "Lane did not produce a decisive result."

    def _uses_fixed_minif2f_prelude(self) -> bool:
        return "MiniF2F.ProblemImports" in self.verifier.prelude

    @staticmethod
    def _fixed_minif2f_lean_context() -> str:
        return (
            "Lean environment guardrails:\n"
            "- The miniF2F common prelude is already provided internally.\n"
            "- Do not output any import or open lines.\n"
            "- Generate only the proof body after `:= by`, or the target theorem with its body.\n"
            "- The evaluation is about whether the model can fill the proof after `by`, not whether it can guess imports."
        )

    def _compact_failure_context(self, state: GraphState) -> dict[str, str]:
        verifier_result = state["verifier_result"]
        context = {
            "failure_reason": self._failure_reason(state),
            "informal": state.get("current_informal", ""),
            "lean_error_log": self._trial_error_log(state),
        }
        return self._without_empty_values(context)

    @classmethod
    def _with_compact_lean_error_log(cls, verifier_result: dict[str, Any]) -> dict[str, Any]:
        return {
            **verifier_result,
            "lean_error_log": cls._extract_core_lean_error_log(
                verifier_result.get("lean_error_log", "")
            ),
        }

    @staticmethod
    def _extract_core_lean_error_log(log: str, max_error_blocks: int = 8) -> str:
        if not log:
            return ""

        lines = [line.rstrip() for line in log.splitlines()]
        compact: list[str] = []

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(("✔", "✖")):
                compact.append(stripped)
                if stripped.startswith("✖"):
                    break
                continue
            if stripped.startswith(
                (
                    "Build completed successfully",
                    "Generated Lean contains",
                    "No Lean code was generated",
                    "Lean generation model failed",
                    "lake build timed out",
                    "Proof audit failed",
                )
            ):
                compact.append(stripped)
                break

        kept_error_blocks = 0
        include_following_detail = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                include_following_detail = False
                continue
            if stripped.startswith(("trace:", ".> ", "LEAN_PATH=")):
                include_following_detail = False
                continue
            if stripped.startswith(("error:", "warning:")):
                if kept_error_blocks >= max_error_blocks:
                    continue
                compact.append(stripped)
                kept_error_blocks += 1
                include_following_detail = True
                continue
            if include_following_detail and (
                line.startswith("  ")
                or stripped.startswith(("Hint:", "numerals are polymorphic"))
            ):
                compact.append(stripped)
                continue
            include_following_detail = False

        if not compact:
            return "\n".join(lines[:20]).strip()

        deduped: list[str] = []
        for line in compact:
            if line not in deduped:
                deduped.append(line)
        return "\n".join(deduped)

    @classmethod
    def _orchestrator_trial_memory_list(
        cls, records: list[TrialMemoryRecord]
    ) -> list[dict[str, str]]:
        return [cls._orchestrator_trial_memory(record) for record in records]

    @classmethod
    def _orchestrator_trial_memory(cls, record: TrialMemoryRecord) -> dict[str, str]:
        return cls._without_empty_values(
            {
                "informal": record.get("informal", ""),
                "lean_error_log": record.get("lean_error_log", ""),
                "retry_plan": record.get("retry_plan", ""),
            }
        )

    @classmethod
    def _compact_trial_feedback(
        cls, record: TrialMemoryRecord | list[TrialMemoryRecord]
    ) -> dict[str, str]:
        records = record if isinstance(record, list) else [record]
        latest = records[-1]
        unavailable_imports = cls._unavailable_imports_from_memory(records)
        import_note = ""
        if unavailable_imports:
            import_note = (
                "Failed imports already tried; do not use these import lines again: "
                + ", ".join(f"`{name}`" for name in unavailable_imports)
            )
        return cls._without_empty_values(
            {
                "failed_imports_to_avoid": import_note,
                "lean_error_log": cls._sanitize_feedback_for_lean_generator(
                    latest.get("lean_error_log", "")
                ),
                "retry_plan": cls._sanitize_feedback_for_lean_generator(
                    latest.get("retry_plan", "")
                ),
            }
        )

    @staticmethod
    def _sanitize_feedback_for_lean_generator(value: str) -> str:
        return value

    @classmethod
    def _unavailable_imports_from_memory(
        cls, records: list[TrialMemoryRecord]
    ) -> list[str]:
        imports: list[str] = []
        marker = "imports unavailable module `"
        for record in records:
            log = record.get("lean_error_log", "")
            start = 0
            while True:
                index = log.find(marker, start)
                if index < 0:
                    break
                name_start = index + len(marker)
                name_end = log.find("`", name_start)
                if name_end < 0:
                    break
                name = log[name_start:name_end].strip()
                if name and name not in imports:
                    imports.append(name)
                start = name_end + 1
        return imports

    @staticmethod
    def _without_empty_values(values: dict[str, str]) -> dict[str, str]:
        return {key: value for key, value in values.items() if value}

    @staticmethod
    def _fallback_retry_plan(error: str) -> str:
        return (
            "Retry planner failed to produce a plan. Keep the next attempt narrow: "
            "fix the latest Lean error directly, preserve the theorem statement exactly, "
            "avoid placeholders, and use a shorter proof if possible. "
            f"Planner error: {error}"
        )

    @staticmethod
    def _is_success(state: GraphState) -> bool:
        return bool(state["verifier_result"]["success"])

    @staticmethod
    def _judge_feedback(state: GraphState) -> str:
        judge_result = state.get("judge_result")
        if not judge_result:
            return ""
        if judge_result.get("equivalent", False):
            return ""
        return (
            "Judge rejected candidate: "
            f"equivalent={judge_result.get('equivalent', False)}; "
            f"score={judge_result.get('score', 0.0)}; "
            f"rationale={judge_result.get('rationale', '')}"
        )

    @classmethod
    def _trial_error_log(cls, state: GraphState) -> str:
        verifier_result = state["verifier_result"]
        parts = []
        if not verifier_result.get("success", False):
            parts.append(verifier_result.get("lean_error_log", ""))
        parts.append(cls._judge_feedback(state))
        return "\n".join(part for part in parts if part).strip()

    @traceable(name="orchestrator.strategy", run_type="llm")
    def _complete_orchestrator_prompt(
        self,
        prompt_name: str,
        prompt_version: str,
        prompt_inputs: dict[str, Any],
        prompt_text: str,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self._complete_prompt(prompt_name, prompt_version, prompt_inputs, prompt_text)

    @traceable(name="nature_agent.informal", run_type="llm")
    def _complete_nature_prompt(
        self,
        prompt_name: str,
        prompt_version: str,
        prompt_inputs: dict[str, Any],
        prompt_text: str,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self._complete_prompt(prompt_name, prompt_version, prompt_inputs, prompt_text)

    @traceable(name="lean_generator.lean_code", run_type="llm")
    def _complete_lean_prompt(
        self,
        prompt_name: str,
        prompt_version: str,
        prompt_inputs: dict[str, Any],
        prompt_text: str,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self._complete_prompt(prompt_name, prompt_version, prompt_inputs, prompt_text)

    @traceable(name="retry_planner.retry_plan", run_type="llm")
    def _complete_retry_prompt(
        self,
        prompt_name: str,
        prompt_version: str,
        prompt_inputs: dict[str, Any],
        prompt_text: str,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self._complete_prompt(prompt_name, prompt_version, prompt_inputs, prompt_text)

    @traceable(name="false_judgment_agent.assess", run_type="llm")
    def _complete_false_judgment_prompt(
        self,
        prompt_name: str,
        prompt_version: str,
        prompt_inputs: dict[str, Any],
        prompt_text: str,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self._complete_prompt(prompt_name, prompt_version, prompt_inputs, prompt_text)

    def _complete_prompt(
        self,
        prompt_name: str,
        prompt_version: str,
        prompt_inputs: dict[str, Any],
        prompt_text: str,
    ) -> str:
        prompt = BuiltPrompt(
            name=prompt_name,
            version=prompt_version,
            inputs=prompt_inputs,
            text=prompt_text,
        )
        return self.model_client.complete(prompt)

    @staticmethod
    def _trial_label(state: GraphState) -> str:
        return f"{state.get('strategy_round', 1)}-{state.get('trial_index', 1)}"

    @staticmethod
    def _trace_extra(
        *,
        name: str,
        strategy_round: int,
        phase: str,
        trial_index: int | None = None,
        trial_label: str | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "strategy_round": strategy_round,
            "phase": phase,
        }
        tags = [f"round:{strategy_round}", f"phase:{phase}"]
        if trial_index is not None:
            metadata["trial_index"] = trial_index
            tags.append(f"trial:{trial_index}")
        if trial_label is not None:
            metadata["trial_label"] = trial_label
            tags.append(f"trial_label:{trial_label}")
        return {"name": name, "metadata": metadata, "tags": tags}


class SingleOrchestrator:
    def __init__(
        self,
        model_client: ModelClient | None = None,
        prompts: PromptRegistry | None = None,
        verifier: LakeVerifier | None = None,
        memory: JsonlTrialMemory | None = None,
        judge_model_client: ModelClient | None = None,
        judge_context: JudgeContext | None = None,
        project_root: Path | None = None,
        max_trials: int = 3,
        max_strategy_rounds: int = 3,
    ):
        self.project_root = project_root or Path.cwd()
        self.judge_context = None
        self.runtime = SingleOrchestratorRuntime(
            model_client=model_client or make_default_model_client(self.project_root),
            prompts=prompts or PromptRegistry.defaults(),
            verifier=verifier or LakeVerifier(self.project_root),
            memory=memory or JsonlTrialMemory(self.project_root / ".runs" / "trial_memory.jsonl"),
            project_root=self.project_root,
            judge=None,
            judge_context=None,
            max_trials=max_trials,
            max_strategy_rounds=max_strategy_rounds,
        )

    @traceable(name="single_pipeline.run", run_type="chain")
    def run(
        self,
        problem: str,
        formal_statement: str = "",
        allowed_imports: list[str] | tuple[str, ...] | None = None,
        lean_header: str = "",
        expected_false_transform: str = "",
        expected_false_fix: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        if os.getenv("DUAL_ARC_SEARCH_ARCHITECTURE", "two_goal") == "two_goal":
            from .goal_search import GoalSearch
            return GoalSearch(model_client=self.runtime.model_client,
                verifier=self.runtime.verifier,
                memories={"prove":self.runtime.memory,"disprove":self.runtime.memory},
                mode="single",max_trials=self.runtime.max_trials,
                max_rounds=self.runtime.max_strategy_rounds).run(
                    problem=problem,formal_statement=formal_statement)
        initial_state: GraphState = {"problem": problem}
        if formal_statement:
            initial_state["formal_statement"] = formal_statement
        if expected_false_fix:
            initial_state["expected_false_fix"] = list(expected_false_fix)
        if expected_false_transform:
            initial_state["expected_false_transform"] = expected_false_transform
        if allowed_imports:
            initial_state["allowed_imports"] = list(allowed_imports)
        if lean_header:
            initial_state["lean_header"] = lean_header
        try:
            from .graph import build_graph
        except ImportError:
            final_state = self._run_without_langgraph(initial_state)
        else:
            graph = build_graph(self.runtime)
            final_state = graph.invoke(
                initial_state,
                config={"recursion_limit": self._recursion_limit()},
            )
        return self._to_result(final_state)

    def _recursion_limit(self) -> int:
        max_attempts = self.runtime.max_trials * self.runtime.max_strategy_rounds
        # A failed attempt can visit nature_agent, lean_generator, verifier,
        # retry_planner, trial_recorder, and routing nodes. Add room for
        # orchestrator strategy rounds and LangGraph bookkeeping.
        return max(25, 10 + max_attempts * 8 + self.runtime.max_strategy_rounds * 2)

    def _run_without_langgraph(self, state: GraphState) -> GraphState:
        state.update(self.runtime.create_strategy(state))
        while True:
            state.update(self.runtime.create_nature_solution(state))
            state.update(self.runtime.generate_lean(state))
            state.update(self.runtime.verify_lean(state))
            if not self.runtime._is_success(state):
                state.update(self.runtime.create_retry_plan(state))
            state.update(self.runtime.record_trial(state))
            if state.get("done"):
                return state
            if state.get("needs_false_decision"):
                state.update(self.runtime.decide_false_after_round(state))
                if state.get("done"):
                    return state
            if state.get("needs_restrategy"):
                state.update(self.runtime.create_strategy(state))

    @staticmethod
    def _to_result(state: GraphState) -> dict[str, Any]:
        verifier_result = state.get("verifier_result", {})
        final_status = state.get("final_status", "")
        success = bool(verifier_result.get("success", False))
        judge_result = state.get("judge_result", {})
        if not final_status:
            final_status = "solved" if success else "not_solved"
        return {
            "mode": "single",
            "success": success,
            "final_status": final_status,
            "decision": final_status,
            "rationale": state.get("false_rationale", ""),
            "trials": len(state.get("trial_memory", [])),
            "proof_trials": len(state.get("trial_memory", [])),
            "falsity_opinions": 0,
            "rounds": state.get("strategy_round", 1),
            "trial_memory": state.get("trial_memory", []),
            "verifier_result": verifier_result,
            "judge_result": judge_result,
            "false_evidence": state.get("false_evidence", "none"),
            "correction_to_true": state.get("correction_to_true", ""),
        }
