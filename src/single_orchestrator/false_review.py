from __future__ import annotations

import json
import os
from contextlib import nullcontext
from copy import deepcopy
from typing import Any

from .model import ModelClient, ModelClientError
from .prompts import BuiltPrompt
from .trace import traceable


REASON_PROMPT = """System:
Determine whether the complete trial records establish that the attempted
statement is mathematically false. Read the natural-language reasoning, Lean
code, formal state, verifier result, and any Falsifier criticism together. If it does, explain precisely which part
fails and why, using a counterexample satisfying every hypothesis or a concrete
mathematical contradiction when available. Lean syntax, tactic, library errors,
timeouts, and failed proof attempts alone do not establish falsity.
Do not infer the dataset author's mutation or hidden answer. Do not propose or
recover a repair. If evidence is insufficient, set is_false to false; this means
falsity is not established, not that the statement has been proved true.
Return JSON only, with exactly two keys: is_false (boolean) and reason (string).

User:
Three-trial memory:
{memory}

Return exactly:
{{"is_false": true_or_false, "reason": "concrete mathematical reason or explanation of insufficient evidence"}}
"""

# Both architectures receive identical diagnostic instructions. The separate
# schema supplies the same evidence as two explicitly labelled memory sections.
DUAL_REASON_PROMPT = REASON_PROMPT.replace(
    "Three-trial memory:\n{memory}",
    "Truth memory:\n{truth_memory}\n\nFalsifier memory:\n{falsifier_memory}",
)

CHECK_PROMPT = """System:
You are the final conservative checker. Compare the submitted mathematical
reason with the private reference falsity reason. Return false only when
is_false is true and the explanation identifies the same mathematical defect
in meaning. Exact wording or the exact reference witness is not required; a
valid alternative witness demonstrating the same defect is acceptable.
Do not require a repair, reconstruction of the original statement, or recovery
of the author's edit. Do not fill in mathematical arguments absent from the
diagnosis. Generic claims that the statement is wrong, proof failures, Lean
errors, incomplete arguments, or unrelated defects must return next_round.
Return JSON only.

User:
Diagnosis:
{claim}

Private reference falsity reason:
{key}

Return exactly:
{{"decision": "false" or "next_round", "why": "short comparison reason"}}
"""


def _reason_trace_input(values: dict[str, Any]) -> dict[str, Any]:
    return {"memory": values.get("memory", [])}


def _dual_reason_trace_input(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "truth_memory": values.get("truth_memory", []),
        "falsifier_memory": values.get("falsifier_memory", []),
    }


def _reason_trace_output(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"is_false": False, "reason": "Invalid diagnosis."}
    return {"is_false": value.get("is_false") is True,
            "reason": value.get("reason", "")}


def _check_trace_input(values: dict[str, Any]) -> dict[str, Any]:
    return {"claim": values.get("claim", {}), "key": values.get("key", {})}


def _check_trace_output(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"decision": "next_round", "why": "Invalid output."}
    return {
        "decision": value.get("decision", "next_round"),
        "why": value.get("why", ""),
    }


class FalseReviewRuntime:
    """Minimal two-model False review used by the isolated test environment."""

    def __init__(self, model_client: ModelClient):
        self.model_client = model_client

    def review(
        self,
        *,
        memory: list[dict[str, Any]],
        answer_key: dict[str, Any],
    ) -> dict[str, Any]:
        if len(memory) != 3:
            return {
                "decision": "next_round",
                "why": "Exactly three trial memories are required.",
                "claim": {"is_false": False, "reason": "Insufficient trial memory."},
            }
        with self._trace_context():
            claim = self.reason(memory=self._clean_memory(memory))
        with self._trace_context():
            decision = self.check(claim=claim, key=self._clean_key(answer_key))
        return {**decision, "claim": claim}

    def review_dual(
        self,
        *,
        truth_memory: list[dict[str, Any]],
        falsifier_memory: list[dict[str, Any]],
        answer_key: dict[str, Any],
    ) -> dict[str, Any]:
        if len(truth_memory) != 3:
            return {
                "decision": "next_round",
                "why": "Exactly three truth trial memories are required.",
                "claim": {"is_false": False, "reason": "Insufficient trial memory."},
            }
        if not falsifier_memory:
            return {
                "decision": "next_round",
                "why": "At least one Falsifier memory is required.",
                "claim": {"is_false": False, "reason": "Insufficient trial memory."},
            }
        with self._trace_context():
            claim = self.reason_dual(
                truth_memory=self._clean_memory(truth_memory),
                falsifier_memory=self._clean_memory(falsifier_memory),
            )
        with self._trace_context():
            decision = self.check(claim=claim, key=self._clean_key(answer_key))
        return {**decision, "claim": claim}

    @traceable(
        name="false_reason",
        run_type="llm",
        process_inputs=_reason_trace_input,
        process_outputs=_reason_trace_output,
    )
    def reason(self, *, memory: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = BuiltPrompt(
            name="false_reason",
            version="v4-full-trial-reason-only",
            inputs={"memory": memory},
            text=REASON_PROMPT.format(
                memory=json.dumps(memory, ensure_ascii=False, indent=2)
            ),
        )
        try:
            raw = self.model_client.complete(prompt)
            parsed = self._parse_json(raw)
        except (ModelClientError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return {"is_false": False, "reason": f"Reason model failed: {exc}"}

        reason = str(parsed.get("reason", "")).strip()
        is_false = parsed.get("is_false") is True and bool(reason)
        return {"is_false": is_false, "reason": reason}

    @traceable(
        name="false_reason",
        run_type="llm",
        process_inputs=_dual_reason_trace_input,
        process_outputs=_reason_trace_output,
    )
    def reason_dual(
        self,
        *,
        truth_memory: list[dict[str, Any]],
        falsifier_memory: list[dict[str, Any]],
    ) -> dict[str, Any]:
        prompt = BuiltPrompt(
            name="false_reason",
            version="v5-dual-full-trial-reason-only",
            inputs={
                "truth_memory": truth_memory,
                "falsifier_memory": falsifier_memory,
            },
            text=DUAL_REASON_PROMPT.format(
                truth_memory=json.dumps(
                    truth_memory, ensure_ascii=False, indent=2
                ),
                falsifier_memory=json.dumps(
                    falsifier_memory, ensure_ascii=False, indent=2
                ),
            ),
        )
        try:
            raw = self.model_client.complete(prompt)
            parsed = self._parse_json(raw)
        except (ModelClientError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return {"is_false": False, "reason": f"Reason model failed: {exc}"}

        reason = str(parsed.get("reason", "")).strip()
        is_false = parsed.get("is_false") is True and bool(reason)
        return {"is_false": is_false, "reason": reason}

    @traceable(
        name="false_check",
        run_type="llm",
        process_inputs=_check_trace_input,
        process_outputs=_check_trace_output,
    )
    def check(
        self,
        *,
        claim: dict[str, Any],
        key: dict[str, Any],
    ) -> dict[str, str]:
        if (
            claim.get("is_false") is not True
            or not str(claim.get("reason", "")).strip()
            or not key.get("reason")
        ):
            return {
                "decision": "next_round",
                "why": "Diagnosis or private key is incomplete or uncertain.",
            }

        prompt = BuiltPrompt(
            name="false_check",
            version="v2-falsity-reason-match",
            inputs={"claim": claim, "key": key},
            text=CHECK_PROMPT.format(
                claim=json.dumps(claim, ensure_ascii=False, indent=2),
                key=json.dumps(key, ensure_ascii=False, indent=2),
            ),
        )
        try:
            raw = self.model_client.complete(prompt)
            parsed = self._parse_json(raw)
        except (ModelClientError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return {"decision": "next_round", "why": f"Check model failed: {exc}"}

        decision = str(parsed.get("decision", "next_round")).strip()
        why = str(parsed.get("why", "")).strip()
        if decision != "false" or not why:
            return {"decision": "next_round", "why": why or "Match was not proven."}
        return {"decision": "false", "why": why}

    @staticmethod
    def _clean_key(answer_key: dict[str, Any]) -> dict[str, Any]:
        # Existing CLI label plumbing carries the private reference through
        # transform. New reason-only datasets must populate it with a mathematical
        # falsity explanation, not merely a mutation description.
        return {"reason": str(answer_key.get("reason") or answer_key.get("transform") or "").strip()}

    @staticmethod
    def _clean_memory(memory: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Preserve every stored Trial field, including empty values and nested
        # verifier data. No LLM summary, field selection, or truncation here.
        return deepcopy(memory)

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()[1:]
            if lines and lines[-1].strip() == "```":
                lines.pop()
            text = "\n".join(lines).strip()
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise TypeError("model output must be a JSON object")
        return parsed

    @staticmethod
    def _trace_context():
        project = os.getenv("DUAL_ARC_FALSE_REVIEW_PROJECT", "").strip()
        if not project:
            return nullcontext()
        try:
            from langsmith import tracing_context
        except ImportError:
            return nullcontext()
        return tracing_context(enabled=True, project_name=project)
