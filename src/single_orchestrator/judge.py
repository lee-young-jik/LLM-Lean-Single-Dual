from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from .model import ModelClient
from .prompts import BuiltPrompt
from .proofnet import ProofNetExample
from .state import JudgeContext
from .trace import traceable


JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class JudgeResult:
    equivalent: bool
    score: float
    rationale: str
    raw_response: str

    def to_json(self) -> dict[str, Any]:
        return {
            "equivalent": self.equivalent,
            "score": self.score,
            "rationale": self.rationale,
            "raw_response": self.raw_response,
        }


class LLMJudge:
    def __init__(self, model_client: ModelClient):
        self.model_client = model_client

    def judge(self, example: ProofNetExample, candidate_lean: str) -> JudgeResult:
        return self.judge_fields(
            formal_statement=example.formal_statement,
            candidate_lean=candidate_lean,
        )

    def judge_context(
        self, context: JudgeContext, candidate_lean: str
    ) -> JudgeResult:
        return self.judge_fields(
            formal_statement=context["formal_statement"],
            candidate_lean=candidate_lean,
        )

    def judge_fields(
        self,
        *,
        formal_statement: str,
        candidate_lean: str,
    ) -> JudgeResult:
        prompt = BuiltPrompt(
            name="llm_judge.proofnet_equivalence",
            version="v1",
            inputs={
                "gold_formal": formal_statement,
                "candidate_lean": candidate_lean,
            },
            text=self._prompt_text(
                formal_statement=formal_statement,
                candidate_lean=candidate_lean,
            ),
        )
        raw = self._complete_judge_prompt(
            prompt.name, prompt.version, prompt.inputs, prompt.text
        )
        payload = _parse_json_response(raw)
        return JudgeResult(
            equivalent=bool(payload.get("equivalent", False)),
            score=float(payload.get("score", 0.0)),
            rationale=str(payload.get("rationale", "")),
            raw_response=raw,
        )

    @staticmethod
    def _prompt_text(
        *,
        formal_statement: str,
        candidate_lean: str,
    ) -> str:
        return (
            "System:\n"
            "You are a strict Lean theorem equivalence judge. Compare the candidate Lean "
            "statement/proof against the gold ProofNet formal statement. The candidate has "
            "already passed lake build, so judge semantic alignment, not syntax alone. "
            "Return JSON only.\n\n"
            "User:\n"
            f"Gold formal statement:\n{formal_statement}\n\n"
            f"Candidate Lean code:\n{candidate_lean}\n\n"
            "Return exactly this JSON shape:\n"
            '{"equivalent": boolean, "score": number, "rationale": string}\n'
            "Use score 1.0 for clearly equivalent, 0.5 for partially aligned, and 0.0 "
            "for not equivalent."
        )

    @traceable(name="semantic_judge.equivalence_check", run_type="llm")
    def _complete_judge_prompt(
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


def _parse_json_response(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fence = JSON_FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise
