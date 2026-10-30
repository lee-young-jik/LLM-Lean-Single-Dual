from __future__ import annotations

from typing import NotRequired, TypedDict


class VerifierResult(TypedDict):
    success: bool
    formal_state: str
    lean_error_log: str
    returncode: int | None
    command: list[str]
    timed_out: bool


class JudgeContext(TypedDict):
    formal_statement: str


class JudgeResult(TypedDict):
    equivalent: bool
    score: float
    rationale: str
    raw_response: str


class TrialMemoryRecord(TypedDict):
    strategy: NotRequired[str]
    lean_strategy: NotRequired[str]
    lean_error_summary: NotRequired[str]
    lean_error_summary_source: NotRequired[str]
    model_lean_error_summary: NotRequired[str]
    repair_error: NotRequired[str]
    lean_code: NotRequired[str]
    verifier_result: NotRequired[dict]
    judge_result: NotRequired[dict]
    strategy_round: NotRequired[int]
    lane: NotRequired[str]
    trial_index: NotRequired[int]
    informal: str
    formal_state: str
    lean_error_log: str
    retry_plan: str


class GraphState(TypedDict):
    problem: str
    formal_statement: NotRequired[str]
    expected_false_fix: NotRequired[list[str]]
    expected_false_transform: NotRequired[str]
    allowed_imports: NotRequired[list[str]]
    lean_header: NotRequired[str]
    lean_context: NotRequired[str]
    strategy: NotRequired[str]
    strategy_round: NotRequired[int]
    trial_index: NotRequired[int]
    trial_memory: NotRequired[list[TrialMemoryRecord]]
    current_informal: NotRequired[str]
    current_lean_code: NotRequired[str]
    current_retry_plan: NotRequired[str]
    model_error_log: NotRequired[str]
    verifier_result: NotRequired[VerifierResult]
    judge_result: NotRequired[JudgeResult]
    needs_restrategy: NotRequired[bool]
    needs_false_decision: NotRequired[bool]
    done: NotRequired[bool]
    final_status: NotRequired[str]
    false_evidence: NotRequired[str]
    false_rationale: NotRequired[str]
    correction_to_true: NotRequired[str]
    final_restrategy: NotRequired[str]
