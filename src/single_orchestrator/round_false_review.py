"""One independent Lean refutation attempt at the end of a failed round."""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
from typing import Any
from .formal_false_review import FormalFalseReviewRuntime, refutation_target
from .false_review import FalseReviewRuntime
from .model import ModelClient, ModelClientError
from .prompts import BuiltPrompt
from .verifier import LakeVerifier

REFUTATION_INSTRUCTIONS = """System:
You are the False Reasoner. Read the complete trial records and attempt a Lean
proof of the fixed refutation target. Trial records contain natural-language
reasoning, Lean proof attempts, formal states, verifier results and retry plans.
Any Falsifier criticism is an unverified proposal, not an assumption.
A failed proof, Lean error or timeout alone is not evidence of mathematical
falsity. Find a genuine counterexample satisfying ALL hypotheses, or another
sound argument showing that the original statement does not hold.
The target negates the WHOLE original statement, including all quantified
variables and hypotheses. Do not change the target, add assumptions, or prove
only a corrected statement. Double negation is intentional when the original
conclusion is itself negated. Do not add a further negation.
Return JSON with exactly one key: {"lean_proof": "Lean tactic proof body"}.
Use only the body after := by. No imports, declarations, commands, sorry, admit,
axioms, native_decide, unsafe constructs, or run_tac. If no supported
refutation can be constructed, return an empty lean_proof. Do not use tools or
look for hidden labels, stored refutations or reference answers.
This is one fresh attempt. No previous refutation outputs or errors are supplied.

User:
"""

class RoundFalseReviewRuntime(FormalFalseReviewRuntime):
    def __init__(self, model_client: ModelClient, verifier: LakeVerifier,
                 audit_path: Path | None = None):
        super().__init__(model_client, verifier)
        self.audit_path = audit_path

    def review(self, *, formal_statement: str, memory: list[dict[str, Any]],
               strategy_round: int = 1) -> dict[str, Any]:
        try:
            target = refutation_target(formal_statement.replace('\\n', '\n'))
        except ValueError as exc:
            result = self.verify_claim(claim={'lean_proof': '', 'why': str(exc)}, target='')
        else:
            inputs = {'memory': deepcopy(memory), 'refutation_target': target}
            prompt = BuiltPrompt(name='false_reason', version='v5-round-lean-refutation',
                                 inputs=inputs, text=REFUTATION_INSTRUCTIONS+
                                 json.dumps(inputs, ensure_ascii=False, indent=2)+
                                 '\nVerifier prelude:\n'+self.verifier.prelude)
            try:
                parsed = FalseReviewRuntime._parse_json(self.model_client.complete(prompt))
                body = parsed.get('lean_proof', '')
                if not isinstance(body, str):
                    raise TypeError('lean_proof must be a string')
                claim = {'lean_proof': body}
            except (ModelClientError, ValueError, TypeError) as exc:
                claim = {'lean_proof': '', 'why': f'Refutation generation failed: {exc}'}
            result = self.verify_claim(claim=claim, target=target)
        # Audit is a side channel only: never read it as next-round model input.
        if self.audit_path is not None:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps({'strategy_round':strategy_round, **result},
                                        ensure_ascii=False, default=str)+'\n')
        return result
