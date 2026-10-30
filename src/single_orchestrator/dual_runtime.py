from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Literal

from .judge import LLMJudge
from .false_review import FalseReviewRuntime
from .round_false_review import RoundFalseReviewRuntime
from .lean_code import complete_lean_source, lean_generation_rejection_reason
from .memory import JsonlTrialMemory
from .model import ModelClient, ModelClientError, make_default_model_client
from .prompts import BuiltPrompt, PromptRegistry
from .state import JudgeContext, TrialMemoryRecord
from .trace import traceable
from .verifier import LakeVerifier


LaneName = Literal["truth", "falsity"]


@dataclass
class LaneResult:
    success: bool
    trials: int
    memory: list[TrialMemoryRecord]
    current_lean_code: str
    verifier_result: dict[str, Any]
    judge_result: dict[str, Any]
    retry_plan: str


@dataclass
class FalsifierOpinion:
    trials: int
    memory: list[TrialMemoryRecord]


@dataclass
class DualOrchestratorRuntime:
    model_client: ModelClient
    prompts: PromptRegistry
    verifier: LakeVerifier
    truth_memory: JsonlTrialMemory
    falsity_memory: JsonlTrialMemory
    judge: LLMJudge | None = None
    judge_context: JudgeContext | None = None
    max_trials: int = 3
    max_rounds: int = 3

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
            return GoalSearch(model_client=self.model_client,verifier=self.verifier,
                memories={"prove":self.truth_memory,"disprove":self.falsity_memory},
                mode="dual",max_trials=self.max_trials,max_rounds=self.max_rounds).run(
                    problem=problem,formal_statement=formal_statement)
        round_summaries: list[dict[str, Any]] = []
        total_truth_trials = 0
        total_falsity_trials = 0
        final_truth: LaneResult | None = None
        final_falsity: FalsifierOpinion | None = None
        audit_memory: list[TrialMemoryRecord] = []
        decision_truth_memory: list[TrialMemoryRecord] = []
        decision_falsity_memory: list[TrialMemoryRecord] = []
        decision: dict[str, Any] = {
            "decision": "unknown",
            "rationale": "No round was executed.",
            "false_evidence": "none",
        }

        for round_index in range(1, self.max_rounds + 1):
            truth_strategy, falsity_strategy = self.run_strategy_pair(
                problem=problem,
                formal_statement=formal_statement,
                lean_header=lean_header,
                round_summaries=round_summaries,
                strategy_round=round_index,
                langsmith_extra=self._trace_extra(
                    name=f"{round_index} orchestrator.strategy_pair.parallel",
                    strategy_round=round_index,
                    phase="dual_orchestrator.strategy_pair.parallel",
                ),
            )
            truth_result = self.run_truth_lane(
                lane="truth",
                problem=problem,
                formal_statement=formal_statement,
                allowed_imports=allowed_imports or (),
                lean_header=lean_header,
                strategy=truth_strategy,
                strategy_round=round_index,
                langsmith_extra=self._trace_extra(
                    name=f"{round_index} truth_lane",
                    strategy_round=round_index,
                    lane="truth",
                    phase="truth_lane",
                ),
            )
            total_truth_trials += truth_result.trials
            final_truth = truth_result
            decision_truth_memory.extend(truth_result.memory)
            audit_memory.extend(
                self._audit_records(
                    lane="truth",
                    records=truth_result.memory,
                    start_index=total_truth_trials - truth_result.trials + 1,
                )
            )

            falsity_result = self.run_falsity_lane(
                problem=problem,
                strategy=falsity_strategy,
                strategy_round=round_index,
                langsmith_extra=self._trace_extra(
                    name=f"{round_index}-F falsity_lane",
                    strategy_round=round_index,
                    lane="falsity",
                    phase="falsity_lane",
                    trial_label=f"{round_index}-F",
                ),
            )
            total_falsity_trials += falsity_result.trials
            final_falsity = falsity_result
            decision_falsity_memory.extend(falsity_result.memory)
            audit_memory.extend(
                self._audit_records(
                    lane="falsity",
                    records=falsity_result.memory,
                    start_index=total_falsity_trials - falsity_result.trials + 1,
                )
            )

            if truth_result.success:
                decision = {
                    "decision": "true",
                    "rationale": (
                        "Truth lane produced a verified proof accepted by Lean. "
                        "Falsity lane was recorded for the round, but no false-only "
                        "decision was needed."
                    ),
                    "false_evidence": "none",
                }
                round_summaries.append(
                    {
                        "round_index": round_index,
                        "truth_success": True,
                        "decision": decision["decision"],
                        "rationale": decision["rationale"],
                        "false_evidence": decision["false_evidence"],
                    }
                )
                break

            decision = self.run_decision_node(
                problem=problem,
                formal_statement=formal_statement,
                lean_header=lean_header,
                truth_memory=truth_result.memory,
                falsity_memory=falsity_result.memory,
                strategy_round=round_index,
                expected_false_transform=expected_false_transform,
                expected_false_fix=expected_false_fix or (),
                langsmith_extra=self._trace_extra(
                    name=f"{round_index} orchestrator.decision",
                    strategy_round=round_index,
                    phase="orchestrator.decision_node",
                ),
            )
            if decision.get("decision") == "true":
                decision = {
                    "decision": "continue",
                    "rationale": (
                        "True decision was downgraded to continue because truth/solved is "
                        "determined only by Lean verification, not by the dual "
                        "orchestrator decision prompt."
                    ),
                    "false_evidence": "none",
                }
            round_summaries.append(
                {
                    "round_index": round_index,
                    "truth_success": truth_result.success,
                    "decision": decision["decision"],
                    "rationale": decision["rationale"],
                    "false_evidence": decision["false_evidence"],
                }
            )

            if decision.get("decision") in {"true", "false"}:
                break

        return {
            "mode": "dual",
            "decision": decision.get("decision", "unknown"),
            "rationale": decision.get("rationale", ""),
            "false_evidence": decision.get("false_evidence", "none"),
            "correction_to_true": decision.get("correction_to_true", ""),
            "final_status": self._final_status(decision),
            "trials": total_truth_trials,
            "proof_trials": total_truth_trials,
            "falsity_opinions": total_falsity_trials,
            "dual_truth_trials": total_truth_trials,
            "dual_falsity_opinions": total_falsity_trials,
            "dual_falsity_trials": total_falsity_trials,
            "rounds": len(round_summaries),
            "trial_memory": audit_memory,
        }

    @traceable(name="orchestrator.strategy_pair.parallel", run_type="chain")
    def run_strategy_pair(
        self,
        *,
        problem: str,
        formal_statement: str,
        lean_header: str,
        round_summaries: list[dict[str, Any]],
        strategy_round: int = 1,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        summaries_snapshot = list(round_summaries)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="dual-strategy") as pool:
            truth_future = pool.submit(
                self.run_truth_orchestrator,
                problem,
                formal_statement,
                lean_header,
                summaries_snapshot,
                strategy_round=strategy_round,
                langsmith_extra=self._trace_extra(
                    name=f"{strategy_round} orchestrator.truth.parallel",
                    strategy_round=strategy_round,
                    lane="truth",
                    phase="orchestrator.truth.parallel",
                ),
            )
            falsity_future = pool.submit(
                self.run_falsity_orchestrator,
                problem,
                formal_statement,
                lean_header,
                summaries_snapshot,
                strategy_round=strategy_round,
                langsmith_extra=self._trace_extra(
                    name=f"{strategy_round} orchestrator.falsity.parallel",
                    strategy_round=strategy_round,
                    lane="falsity",
                    phase="orchestrator.falsity.parallel",
                ),
            )
            return truth_future.result(), falsity_future.result()

    @traceable(name="orchestrator.truth", run_type="chain")
    def run_truth_orchestrator(
        self,
        problem: str,
        formal_statement: str,
        lean_header: str,
        round_summaries: list[dict[str, Any]],
        strategy_round: int = 1,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self.create_truth_strategy(
            problem,
            formal_statement,
            lean_header,
            round_summaries,
            strategy_round=strategy_round,
            langsmith_extra=self._trace_extra(
                name=f"{strategy_round} orchestrator.strategy.truth",
                strategy_round=strategy_round,
                lane="truth",
                phase="dual_orchestrator.truth_strategy",
            ),
        )

    @traceable(name="orchestrator.falsity", run_type="chain")
    def run_falsity_orchestrator(
        self,
        problem: str,
        formal_statement: str,
        lean_header: str,
        round_summaries: list[dict[str, Any]],
        strategy_round: int = 1,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self.create_falsity_strategy(
            problem,
            formal_statement,
            lean_header,
            round_summaries,
            strategy_round=strategy_round,
            langsmith_extra=self._trace_extra(
                name=f"{strategy_round} orchestrator.strategy.falsity",
                strategy_round=strategy_round,
                lane="falsity",
                phase="dual_orchestrator.falsity_strategy",
            ),
        )

    @traceable(name="orchestrator.strategy.truth", run_type="llm")
    def create_truth_strategy(
        self,
        problem: str,
        formal_statement: str,
        lean_header: str,
        round_summaries: list[dict[str, Any]],
        strategy_round: int = 1,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        prompt = self.prompts.build(
            "dual_orchestrator.truth_strategy",
            problem=problem,
            formal_statement=formal_statement,
            lean_header=lean_header,
            round_summaries=round_summaries,
        )
        return self._complete(prompt)

    @traceable(name="orchestrator.strategy.falsity", run_type="llm")
    def create_falsity_strategy(
        self,
        problem: str,
        formal_statement: str,
        lean_header: str,
        round_summaries: list[dict[str, Any]],
        strategy_round: int = 1,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        prompt = self.prompts.build(
            "dual_orchestrator.falsity_strategy",
            problem=problem,
            formal_statement=formal_statement,
            lean_header=lean_header,
            round_summaries=round_summaries,
        )
        return self._complete(prompt)

    def run_lane(
        self,
        *,
        lane: LaneName,
        problem: str,
        formal_statement: str,
        allowed_imports: list[str] | tuple[str, ...],
        lean_header: str,
        strategy: str,
        strategy_round: int = 1,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> LaneResult:
        memory: list[TrialMemoryRecord] = []
        retry_plan = ""
        current_informal = ""
        current_lean_code = ""
        verifier_result: dict[str, Any] = {}
        judge_result: dict[str, Any] = {}

        for trial_index in range(1, self.max_trials + 1):
            trial_label = f"{strategy_round}-{trial_index}"
            current_informal = self.run_nature_agent_node(
                lane=lane,
                problem=problem,
                strategy=strategy,
                previous_trial=memory[-1] if memory else None,
                strategy_round=strategy_round,
                trial_index=trial_index,
                trial_label=trial_label,
                langsmith_extra=self._trace_extra(
                    name=f"{trial_label} nature_agent",
                    strategy_round=strategy_round,
                    lane=lane,
                    phase="nature_agent",
                    trial_index=trial_index,
                    trial_label=trial_label,
                ),
            )
            current_lean_code, model_error_log = self.run_lean_generator_node(
                lane=lane,
                problem=problem,
                informal=current_informal,
                formal_statement=formal_statement,
                allowed_imports=allowed_imports,
                lean_header=lean_header,
                previous_trial=memory[-5:] if memory else None,
                strategy_round=strategy_round,
                trial_index=trial_index,
                trial_label=trial_label,
                langsmith_extra=self._trace_extra(
                    name=f"{trial_label} lean_generator",
                    strategy_round=strategy_round,
                    lane=lane,
                    phase="lean_generator",
                    trial_index=trial_index,
                    trial_label=trial_label,
                ),
            )
            verifier_result = self.run_verifier_node(
                lane=lane,
                lean_code=current_lean_code,
                model_error_log=model_error_log,
                formal_statement=formal_statement,
                strategy_round=strategy_round,
                trial_index=trial_index,
                trial_label=trial_label,
                langsmith_extra=self._trace_extra(
                    name=f"{trial_label} verifier",
                    strategy_round=strategy_round,
                    lane=lane,
                    phase="verifier",
                    trial_index=trial_index,
                    trial_label=trial_label,
                ),
            )
            if self._lane_success(lane, verifier_result, judge_result):
                retry_plan = ""
            else:
                retry_plan = self.run_retry_planner_node(
                    lane=lane,
                    problem=problem,
                    informal=current_informal,
                    verifier_result=verifier_result,
                    judge_result=judge_result,
                    strategy_round=strategy_round,
                    trial_index=trial_index,
                    trial_label=trial_label,
                    langsmith_extra=self._trace_extra(
                        name=f"{trial_label} retry_planner",
                        strategy_round=strategy_round,
                        lane=lane,
                        phase="retry_planner",
                        trial_index=trial_index,
                        trial_label=trial_label,
                    ),
                )

            record = self.make_record(
                informal=current_informal,
                verifier_result=verifier_result,
                judge_result=judge_result,
                retry_plan=retry_plan,
                lean_code=current_lean_code,
            )
            record.update(lane=lane, trial_index=trial_index, strategy_round=strategy_round)
            self._memory_for(lane).append(record)
            memory.append(record)

            if self._lane_success(lane, verifier_result, judge_result):
                return LaneResult(
                    success=True,
                    trials=trial_index,
                    memory=memory,
                    current_lean_code=current_lean_code,
                    verifier_result=verifier_result,
                    judge_result=judge_result,
                    retry_plan=retry_plan,
                )

        return LaneResult(
            success=False,
            trials=len(memory),
            memory=memory,
            current_lean_code=current_lean_code,
            verifier_result=verifier_result,
            judge_result=judge_result,
            retry_plan=retry_plan,
        )

    def run_truth_lane(self, **kwargs: Any) -> LaneResult:
        return self._run_truth_lane_node(**kwargs)

    def run_falsity_lane(self, **kwargs: Any) -> FalsifierOpinion:
        return self._run_falsity_lane_node(**kwargs)

    @traceable(name="truth_lane", run_type="chain")
    def _run_truth_lane_node(
        self, langsmith_extra: dict[str, Any] | None = None, **kwargs: Any
    ) -> LaneResult:
        return self.run_lane(**kwargs)

    @traceable(name="falsity_lane", run_type="chain")
    def _run_falsity_lane_node(
        self, langsmith_extra: dict[str, Any] | None = None, **kwargs: Any
    ) -> FalsifierOpinion:
        return self.run_falsifier_once(
            problem=kwargs["problem"],
            strategy=kwargs["strategy"],
            strategy_round=kwargs.get("strategy_round", 1),
        )

    def run_falsifier_once(
        self,
        *,
        problem: str,
        strategy: str,
        strategy_round: int = 1,
    ) -> FalsifierOpinion:
        trial_label = f"{strategy_round}-F"
        informal = self.run_nature_agent_node(
            lane="falsity",
            problem=problem,
            strategy=strategy,
            previous_trial=None,
            strategy_round=strategy_round,
            trial_index=None,
            trial_label=trial_label,
            langsmith_extra=self._trace_extra(
                name=f"{trial_label} nature_agent",
                strategy_round=strategy_round,
                lane="falsity",
                phase="nature_agent",
                trial_label=trial_label,
            ),
        )
        record: TrialMemoryRecord = {
            "informal": informal,
            "formal_state": "",
            "lean_error_log": "",
            "retry_plan": "",
        }
        self.falsity_memory.append(record)
        return FalsifierOpinion(trials=1, memory=[record])

    def run_decision_node(
        self,
        *,
        problem: str,
        formal_statement: str,
        lean_header: str = "",
        truth_memory: list[TrialMemoryRecord],
        falsity_memory: list[TrialMemoryRecord],
        strategy_round: int = 1,
        expected_false_transform: str = "",
        expected_false_fix: list[str] | tuple[str, ...] = (),
        langsmith_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.decide(
            problem=problem,
            formal_statement=formal_statement,
            lean_header=lean_header,
            truth_memory=truth_memory,
            falsity_memory=falsity_memory,
            strategy_round=strategy_round,
            expected_false_transform=expected_false_transform,
            expected_false_fix=expected_false_fix,
            langsmith_extra=self._trace_extra(
                name=f"{strategy_round} orchestrator.decision",
                strategy_round=strategy_round,
                phase="dual_orchestrator.decision",
            ),
        )

    @traceable(name="nature_agent.informal.truth", run_type="llm")
    def _truth_nature_complete(
        self,
        prompt: BuiltPrompt,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self._complete(prompt)

    @traceable(name="nature_agent.informal.falsity", run_type="llm")
    def _falsity_nature_complete(
        self,
        prompt: BuiltPrompt,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self._complete(prompt)

    @traceable(name="nature_agent", run_type="chain")
    def run_nature_agent_node(
        self,
        *,
        lane: LaneName,
        problem: str,
        strategy: str,
        previous_trial: TrialMemoryRecord | None,
        strategy_round: int = 1,
        trial_index: int | None = None,
        trial_label: str | None = None,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self.create_lane_informal(
            lane=lane,
            problem=problem,
            strategy=strategy,
            previous_trial=previous_trial,
            strategy_round=strategy_round,
            trial_index=trial_index,
            trial_label=trial_label,
        )

    def create_lane_informal(
        self,
        *,
        lane: LaneName,
        problem: str,
        strategy: str,
        previous_trial: TrialMemoryRecord | None,
        strategy_round: int = 1,
        trial_index: int | None = None,
        trial_label: str | None = None,
    ) -> str:
        prefix = "truth" if lane == "truth" else "falsity"
        if previous_trial is None:
            prompt = self.prompts.build(
                f"{prefix}_nature_agent.initial",
                problem=problem,
                strategy=strategy,
            )
        else:
            prompt = self.prompts.build(
                f"{prefix}_nature_agent.retry",
                problem=problem,
                strategy=strategy,
                previous_trial=self._previous_trial_feedback(previous_trial),
            )
        if lane == "truth":
            return self._truth_nature_complete(
                prompt,
                langsmith_extra=self._trace_extra(
                    name=f"{trial_label or strategy_round} nature_agent.informal.truth",
                    strategy_round=strategy_round,
                    lane="truth",
                    phase=prompt.name,
                    trial_index=trial_index,
                    trial_label=trial_label,
                ),
            )
        return self._falsity_nature_complete(
            prompt,
            langsmith_extra=self._trace_extra(
                name=f"{trial_label or strategy_round} nature_agent.informal.falsity",
                strategy_round=strategy_round,
                lane="falsity",
                phase=prompt.name,
                trial_index=trial_index,
                trial_label=trial_label,
            ),
        )

    @traceable(name="lean_generator.lean_code.truth", run_type="llm")
    def _truth_lean_complete(
        self,
        prompt: BuiltPrompt,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self._complete(prompt)

    @traceable(name="lean_generator.lean_code.falsity", run_type="llm")
    def _falsity_lean_complete(
        self,
        prompt: BuiltPrompt,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self._complete(prompt)

    def generate_lane_lean(
        self,
        *,
        lane: LaneName,
        problem: str,
        informal: str,
        formal_statement: str,
        allowed_imports: list[str] | tuple[str, ...] = (),
        lean_header: str = "",
        previous_trial: TrialMemoryRecord | list[TrialMemoryRecord] | None = None,
        strategy_round: int = 1,
        trial_index: int | None = None,
        trial_label: str | None = None,
    ) -> tuple[str, str]:
        prefix = "truth" if lane == "truth" else "falsity"
        prompt = self.prompts.build(
            f"{prefix}_lean_generator.generate",
            problem=problem,
            current_informal=informal,
            formal_statement=formal_statement,
            allowed_imports=list(allowed_imports),
            lean_header=lean_header,
            previous_trial=(
                self._previous_trial_feedback(previous_trial) if previous_trial else {}
            ),
        )
        try:
            if lane == "truth":
                raw = self._truth_lean_complete(
                    prompt,
                    langsmith_extra=self._trace_extra(
                        name=(
                            f"{trial_label or strategy_round} "
                            "lean_generator.lean_code.truth"
                        ),
                        strategy_round=strategy_round,
                        lane="truth",
                        phase=prompt.name,
                        trial_index=trial_index,
                        trial_label=trial_label,
                    ),
                )
            else:
                raw = self._falsity_lean_complete(
                    prompt,
                    langsmith_extra=self._trace_extra(
                        name=(
                            f"{trial_label or strategy_round} "
                            "lean_generator.lean_code.falsity"
                        ),
                        strategy_round=strategy_round,
                        lane="falsity",
                        phase=prompt.name,
                        trial_index=trial_index,
                        trial_label=trial_label,
                    ),
                )
            lean_code = complete_lean_source(
                raw,
                formal_statement,
                preserve_header=not self._uses_fixed_minif2f_prelude(),
            )
            rejection_reason = ""
            if not lean_code.strip():
                rejection_reason = lean_generation_rejection_reason(raw)
            return (
                lean_code,
                rejection_reason,
            )
        except ModelClientError as exc:
            return "", f"{prefix} Lean generation model failed: {exc}"

    def _uses_fixed_minif2f_prelude(self) -> bool:
        return "MiniF2F.ProblemImports" in self.verifier.prelude

    @traceable(name="lean_generator", run_type="chain")
    def run_lean_generator_node(
        self,
        *,
        lane: LaneName,
        problem: str,
        informal: str,
        formal_statement: str,
        allowed_imports: list[str] | tuple[str, ...] = (),
        lean_header: str = "",
        previous_trial: TrialMemoryRecord | list[TrialMemoryRecord] | None = None,
        strategy_round: int = 1,
        trial_index: int | None = None,
        trial_label: str | None = None,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        return self.generate_lane_lean(
            lane=lane,
            problem=problem,
            informal=informal,
            formal_statement=formal_statement,
            allowed_imports=allowed_imports,
            lean_header=lean_header,
            previous_trial=previous_trial,
            strategy_round=strategy_round,
            trial_index=trial_index,
            trial_label=trial_label,
        )

    @traceable(name="verifier.lake_build.truth", run_type="chain")
    def _truth_verify(
        self,
        lean_code: str,
        model_error_log: str,
        formal_statement: str = "",
        langsmith_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._verify(lean_code, model_error_log, formal_statement)

    @traceable(name="verifier.lake_build.falsity", run_type="chain")
    def _falsity_verify(
        self,
        lean_code: str,
        model_error_log: str,
        formal_statement: str = "",
        langsmith_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._verify(lean_code, model_error_log, formal_statement)

    def verify_lane_lean(
        self,
        *,
        lane: LaneName,
        lean_code: str,
        model_error_log: str,
        formal_statement: str = "",
        strategy_round: int = 1,
        trial_index: int | None = None,
        trial_label: str | None = None,
    ) -> dict[str, Any]:
        if lane == "truth":
            return self._truth_verify(
                lean_code,
                model_error_log,
                formal_statement,
                langsmith_extra=self._trace_extra(
                    name=f"{trial_label or strategy_round} verifier.lake_build.truth",
                    strategy_round=strategy_round,
                    lane="truth",
                    phase="verifier.lake_build.truth",
                    trial_index=trial_index,
                    trial_label=trial_label,
                ),
            )
        return self._falsity_verify(
            lean_code,
            model_error_log,
            formal_statement,
            langsmith_extra=self._trace_extra(
                name=f"{trial_label or strategy_round} verifier.lake_build.falsity",
                strategy_round=strategy_round,
                lane="falsity",
                phase="verifier.lake_build.falsity",
                trial_index=trial_index,
                trial_label=trial_label,
            ),
        )

    @traceable(name="verifier", run_type="chain")
    def run_verifier_node(
        self,
        *,
        lane: LaneName,
        lean_code: str,
        model_error_log: str,
        formal_statement: str = "",
        strategy_round: int = 1,
        trial_index: int | None = None,
        trial_label: str | None = None,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.verify_lane_lean(
            lane=lane,
            lean_code=lean_code,
            model_error_log=model_error_log,
            formal_statement=formal_statement,
            strategy_round=strategy_round,
            trial_index=trial_index,
            trial_label=trial_label,
        )

    def _verify(
        self, lean_code: str, model_error_log: str, formal_statement: str = ""
    ) -> dict[str, Any]:
        if model_error_log:
            return {
                "success": False,
                "formal_state": "",
                "lean_error_log": model_error_log,
                "returncode": None,
                "command": [],
                "timed_out": "timed out" in model_error_log,
                "has_prohibited_placeholder": False,
            }
        result = self.verifier.verify(lean_code, formal_statement=formal_statement)
        return {
            **result,
            "lean_error_log": self._extract_core_lean_error_log(
                result.get("lean_error_log", "")
            ),
        }

    @traceable(name="retry_planner.retry_plan.truth", run_type="llm")
    def _truth_retry_complete(
        self,
        prompt: BuiltPrompt,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self._complete(prompt)

    @traceable(name="retry_planner.retry_plan.falsity", run_type="llm")
    def _falsity_retry_complete(
        self,
        prompt: BuiltPrompt,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self._complete(prompt)

    def create_lane_retry_plan(
        self,
        *,
        lane: LaneName,
        problem: str,
        informal: str,
        verifier_result: dict[str, Any],
        judge_result: dict[str, Any],
        strategy_round: int = 1,
        trial_index: int | None = None,
        trial_label: str | None = None,
    ) -> str:
        prefix = "truth" if lane == "truth" else "falsity"
        prompt = self.prompts.build(
            f"{prefix}_retry_planner.plan",
            problem=problem,
            failure_context=self._without_empty_values(
                {
                    "failure_reason": self._failure_reason(verifier_result, judge_result),
                    "informal": informal,
                    "lean_error_log": self._trial_error_log(
                        verifier_result, judge_result
                    ),
                }
            ),
        )
        try:
            if lane == "truth":
                return self._truth_retry_complete(
                    prompt,
                    langsmith_extra=self._trace_extra(
                        name=f"{trial_label or strategy_round} retry_planner.retry_plan.truth",
                        strategy_round=strategy_round,
                        lane="truth",
                        phase=prompt.name,
                        trial_index=trial_index,
                        trial_label=trial_label,
                    ),
                )
            return self._falsity_retry_complete(
                prompt,
                langsmith_extra=self._trace_extra(
                    name=f"{trial_label or strategy_round} retry_planner.retry_plan.falsity",
                    strategy_round=strategy_round,
                    lane="falsity",
                    phase=prompt.name,
                    trial_index=trial_index,
                    trial_label=trial_label,
                ),
            )
        except ModelClientError as exc:
            return self._fallback_retry_plan(lane, str(exc))

    @traceable(name="retry_planner", run_type="chain")
    def run_retry_planner_node(
        self,
        *,
        lane: LaneName,
        problem: str,
        informal: str,
        verifier_result: dict[str, Any],
        judge_result: dict[str, Any],
        strategy_round: int = 1,
        trial_index: int | None = None,
        trial_label: str | None = None,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self.create_lane_retry_plan(
            lane=lane,
            problem=problem,
            informal=informal,
            verifier_result=verifier_result,
            judge_result=judge_result,
            strategy_round=strategy_round,
            trial_index=trial_index,
            trial_label=trial_label,
        )

    def decide(
        self,
        *,
        problem: str,
        formal_statement: str,
        lean_header: str = "",
        truth_memory: list[TrialMemoryRecord],
        falsity_memory: list[TrialMemoryRecord],
        strategy_round: int = 1,
        expected_false_transform: str = "",
        expected_false_fix: list[str] | tuple[str, ...] = (),
        langsmith_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        truth_trials = [dict(record) for record in truth_memory[-3:]]
        falsity_trials = [dict(record) for record in falsity_memory]
        # One shared prompt for Single/Dual; retain full Trial fields and all
        # Falsifier records without injecting a separate natural-language problem.
        review_memory = [dict(record) for record in truth_trials]
        if review_memory and falsity_trials:
            review_memory[-1]["falsifier_memory"] = falsity_trials
        review = RoundFalseReviewRuntime(
            self.model_client, self.verifier,
            audit_path=self.truth_memory.path.with_name("refutation_audit.jsonl"),
        ).review(formal_statement=formal_statement, memory=review_memory,
                 strategy_round=strategy_round)
        success = review.get("decision") == "false"
        return {
            "decision": "false" if success else "continue",
            "rationale": str(review.get("why", "")) if success else "",
            "false_evidence": "lean_verified_negation" if success else "none",
            "correction_to_true": "",
        }

    def _false_agent_assessment(
        self,
        *,
        problem: str,
        formal_statement: str,
        evidence_memory: dict[str, Any],
        strategy_round: int,
    ) -> dict[str, Any]:
        prompt = self.prompts.build(
            "false_judgment_agent.assess",
            problem=problem,
            formal_statement=formal_statement,
            evidence_memory=evidence_memory,
        )
        try:
            raw = self._complete_false_judgment_prompt(prompt)
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
        decision = str(parsed.get("decision", "continue") or "continue")
        if decision not in {"false", "continue", "unknown"}:
            decision = "continue"
        false_evidence = str(parsed.get("false_evidence", "none") or "none")
        correction_to_true = str(parsed.get("correction_to_true", "") or "").strip()
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
        if (
            decision != "false"
            or false_evidence == "none"
            or not correction_to_true
            or not hypotheses_check
            or not conclusion_check
            or uncertainties
        ):
            decision = "continue"
        return {
            "decision": decision,
            "rationale": rationale,
            "false_evidence": false_evidence,
            "correction_to_true": correction_to_true,
            "hypotheses_check": hypotheses_check,
            "conclusion_check": conclusion_check,
            "uncertainties": uncertainties,
        }

    @traceable(name="false_judgment_agent.assess", run_type="llm")
    def _complete_false_judgment_prompt(
        self,
        prompt: BuiltPrompt,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self._complete(prompt)

    def _apply_expected_false_fix_gate(
        self,
        decision: dict[str, Any],
        formal_statement: str,
        expected_false_fix: list[str] | tuple[str, ...],
        evidence_memory: dict[str, Any] | str,
    ) -> dict[str, Any]:
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
        correction = str(decision.get("correction_to_true", "") or "")
        judge = self._judge_correction_to_true(
            formal_statement=formal_statement,
            evidence_memory=evidence_memory,
            false_rationale=str(decision.get("rationale", "") or ""),
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
        return {
            "decision": "continue",
            "rationale": (
                "False decision was downgraded to continue because the correction judge "
                "did not accept the proposed correction_to_true. "
                f"Expected correction: {'; '.join(expected_false_fix)}. "
                f"Proposed correction: {correction}. "
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
        evidence_memory: dict[str, Any] | str,
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
            raw = self._complete_correction_judge_prompt(prompt)
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
        prompt: BuiltPrompt,
        langsmith_extra: dict[str, Any] | None = None,
    ) -> str:
        return self._complete(prompt)

    @staticmethod
    def _final_status(decision: dict[str, Any]) -> str:
        if decision.get("decision") == "true":
            return "solved"
        if decision.get("decision") == "false":
            return "false"
        return "not_solved"

    def judge_truth(self, formal_state: str) -> dict[str, Any]:
        if self.judge is None or self.judge_context is None:
            return {}
        return self.judge.judge_context(self.judge_context, formal_state).to_json()

    def make_record(
        self,
        *,
        informal: str,
        verifier_result: dict[str, Any],
        judge_result: dict[str, Any],
        retry_plan: str,
        lean_code: str = "",
    ) -> TrialMemoryRecord:
        return {
            "informal": informal,
            "lean_code": lean_code,
            "verifier_result": dict(verifier_result),
            "judge_result": dict(judge_result),
            "formal_state": verifier_result.get("formal_state", ""),
            "lean_error_log": self._trial_error_log(verifier_result, judge_result),
            "retry_plan": retry_plan,
        }

    def _memory_for(self, lane: LaneName) -> JsonlTrialMemory:
        return self.truth_memory if lane == "truth" else self.falsity_memory

    @staticmethod
    def _audit_records(
        *, lane: LaneName, records: list[TrialMemoryRecord], start_index: int
    ) -> list[TrialMemoryRecord]:
        return [
            {
                **record,
                "lane": lane,
                "trial_index": start_index + offset,
            }
            for offset, record in enumerate(records)
        ]

    @staticmethod
    def _lane_success(
        lane: LaneName, verifier_result: dict[str, Any], judge_result: dict[str, Any]
    ) -> bool:
        if not verifier_result.get("success"):
            return False
        if lane == "falsity":
            return True
        return True

    @staticmethod
    def _failure_reason(verifier_result: dict[str, Any], judge_result: dict[str, Any]) -> str:
        if not verifier_result.get("success"):
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

    @classmethod
    def _extract_core_lean_error_log(cls, log: str, max_error_blocks: int = 8) -> str:
        from .runtime import SingleOrchestratorRuntime

        return SingleOrchestratorRuntime._extract_core_lean_error_log(
            log, max_error_blocks=max_error_blocks
        )

    @classmethod
    def _previous_trial_feedback(
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

    @classmethod
    def _orchestrator_memory(
        cls, records: list[TrialMemoryRecord]
    ) -> list[dict[str, str]]:
        return [
            cls._without_empty_values(
                {
                    "informal": record.get("informal", ""),
                    "lean_error_log": record.get("lean_error_log", ""),
                    "retry_plan": record.get("retry_plan", ""),
                }
            )
            for record in records
        ]

    @staticmethod
    def _judge_feedback(judge_result: dict[str, Any]) -> str:
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
    def _trial_error_log(
        cls, verifier_result: dict[str, Any], judge_result: dict[str, Any]
    ) -> str:
        parts = []
        if not verifier_result.get("success", False):
            parts.append(verifier_result.get("lean_error_log", ""))
        parts.append(cls._judge_feedback(judge_result))
        return "\n".join(part for part in parts if part).strip()

    @staticmethod
    def _without_empty_values(values: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in values.items() if value}

    @staticmethod
    def _fallback_retry_plan(lane: LaneName, error: str) -> str:
        lane_label = "truth-side" if lane == "truth" else "falsity-side"
        return (
            f"{lane_label} retry planner failed to produce a plan. Keep the next "
            "attempt narrow: use only the latest error, preserve the target exactly, "
            "avoid placeholders, and choose a shorter route if possible. "
            f"Planner error: {error}"
        )

    @staticmethod
    def _strip_json_fence(text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            return "\n".join(lines).strip()
        return stripped

    def _complete(self, prompt: BuiltPrompt) -> str:
        return self.model_client.complete(prompt)

    @staticmethod
    def _trace_extra(
        *,
        name: str,
        strategy_round: int,
        phase: str,
        lane: LaneName | None = None,
        trial_index: int | None = None,
        trial_label: str | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "strategy_round": strategy_round,
            "phase": phase,
        }
        tags = [f"round:{strategy_round}", f"phase:{phase}"]
        if lane is not None:
            metadata["lane"] = lane
            tags.append(f"lane:{lane}")
        if trial_index is not None:
            metadata["trial_index"] = trial_index
            tags.append(f"trial:{trial_index}")
        if trial_label is not None:
            metadata["trial_label"] = trial_label
            tags.append(f"trial_label:{trial_label}")
        return {"name": name, "metadata": metadata, "tags": tags}


class DualOrchestrator:
    def __init__(
        self,
        *,
        model_client: ModelClient | None = None,
        judge_model_client: ModelClient | None = None,
        judge_context: JudgeContext | None = None,
        verifier: LakeVerifier | None = None,
        truth_memory: JsonlTrialMemory | None = None,
        falsity_memory: JsonlTrialMemory | None = None,
        prompts: PromptRegistry | None = None,
        project_root: Path | None = None,
        max_trials: int = 3,
        max_rounds: int = 3,
    ):
        self.project_root = project_root or Path.cwd()
        self.runtime = DualOrchestratorRuntime(
            model_client=model_client or make_default_model_client(self.project_root),
            prompts=prompts or PromptRegistry.defaults(),
            verifier=verifier or LakeVerifier(self.project_root),
            truth_memory=truth_memory
            or JsonlTrialMemory(
                self.project_root / ".runs" / "dual_truth_memory.jsonl",
                lane="truth",
            ),
            falsity_memory=falsity_memory
            or JsonlTrialMemory(
                self.project_root / ".runs" / "dual_falsity_memory.jsonl",
                lane="falsity",
            ),
            judge=None,
            judge_context=None,
            max_trials=max_trials,
            max_rounds=max_rounds,
        )

    @traceable(name="dual_pipeline.run", run_type="chain")
    def run(
        self,
        problem: str,
        formal_statement: str = "",
        allowed_imports: list[str] | tuple[str, ...] | None = None,
        lean_header: str = "",
        expected_false_transform: str = "",
        expected_false_fix: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        return self.runtime.run(
            problem,
            formal_statement=formal_statement,
            allowed_imports=allowed_imports,
            lean_header=lean_header,
            expected_false_transform=expected_false_transform,
            expected_false_fix=expected_false_fix,
        )
