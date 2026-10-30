"""Memory-based refutation with a fixed, closed Lean target (no answer key)."""
from __future__ import annotations

import json
import re
import textwrap
from typing import Any

from .false_review import FalseReviewRuntime
from .model import ModelClient, ModelClientError
from .prompts import BuiltPrompt
from .verifier import LakeVerifier
from .lean_format import normalize_lean_proof


def _without_lean_comments(source: str) -> str:
    """Mask nested comments for the guard, preserving literals and line positions.

    The original source is always sent to Lean. Masking does not approve a proof:
    the compiler and declaration-specific axiom audit remain mandatory.
    """
    out=[];i=0;size=len(source)
    while i<size:
        if source.startswith('--',i):
            end=source.find('\n',i)
            if end<0:end=size
            out.append(' '*(end-i));i=end
        elif source.startswith('/-',i):
            depth=1;out.append('  ');i+=2
            while i<size and depth:
                if source.startswith('/-',i):depth+=1;out.append('  ');i+=2
                elif source.startswith('-/',i):depth-=1;out.append('  ');i+=2
                else:out.append('\n' if source[i]=='\n' else ' ');i+=1
        elif source[i]=='«':
            end=source.find('»',i+1);end=size if end<0 else end+1
            out.append(source[i:end]);i=end
        else:
            raw=re.match(r'r(#+)"',source[i:]) if source[i]=='r' else None
            if raw:
                close='"'+raw.group(1);end=source.find(close,i+len(raw.group(0)))
                end=size if end<0 else end+len(close)
                out.append(source[i:end]);i=end
            elif source[i]=='"':
                start=i;i+=1
                while i<size:
                    if source[i]=='\\':i=min(size,i+2)
                    elif source[i]=='"':i+=1;break
                    else:i+=1
                out.append(source[start:i])
            elif source[i]=="'" and (char:=re.match(r"'(?:\\.|[^\\'\n])'",source[i:])):
                out.append(char.group(0));i+=len(char.group(0))
            else:out.append(source[i]);i+=1
    return ''.join(out)


def refutation_target(statement: str) -> str:
    """Negate the whole theorem, including its universally quantified binders.

    Keep the original binder/goal text intact. Reject unsupported surfaces rather
    than ask a model to invent a target. In particular, no original hypotheses
    remain as assumptions of the refutation theorem.
    """
    match = re.fullmatch(r"\s*theorem\s+([\w'.]+)\s*(.*?)\s*:=\s*by\s*", statement, re.S)
    if not match:
        raise ValueError("Expected a standalone theorem header ending in := by.")
    name, header = match.groups()
    stack: list[str] = []
    pairs = {')': '(', '}': '{', ']': '[', '⦄': '⦃'}
    colon = None
    for i, char in enumerate(header):
        if char in '({[⦃':
            stack.append(char)
        elif char in pairs:
            if not stack or stack.pop() != pairs[char]:
                raise ValueError("Unbalanced theorem header.")
        elif char == ':' and not stack:
            colon = i
            break
    if colon is None:
        raise ValueError("Theorem result type was not found.")
    binders, goal = header[:colon].strip(), header[colon + 1:].strip()
    if not goal or (binders and binders[0] not in '({[⦃'):
        raise ValueError("Unsupported theorem binder syntax.")
    proposition = f"∀ {binders}, {goal}" if binders else goal
    return f"theorem {name}_refutation : ¬ ({proposition}) := by"


class FormalFalseReviewRuntime:
    def __init__(self, model_client: ModelClient, verifier: LakeVerifier):
        self.model_client = model_client
        self.verifier = verifier

    def review(self, *, memory: list[dict[str, Any]],
               problem_memory: dict[str, Any] | None = None,
               previous_refutations: list[dict[str, Any]] | None = None,
               **_ignored: Any) -> dict[str, Any]:
        return self._review(problem_memory=problem_memory,
                            evidence={"memory": FalseReviewRuntime._clean_memory(memory)},
                            previous_refutations=previous_refutations)

    def review_dual(self, *, truth_memory: list[dict[str, Any]],
                    falsifier_memory: list[dict[str, Any]],
                    problem_memory: dict[str, Any] | None = None,
                    previous_refutations: list[dict[str, Any]] | None = None,
                    **_ignored: Any) -> dict[str, Any]:
        return self._review(problem_memory=problem_memory, evidence={
            "truth_memory": FalseReviewRuntime._clean_memory(truth_memory),
            "falsifier_memory": FalseReviewRuntime._clean_memory(falsifier_memory)},
            previous_refutations=previous_refutations)

    def _review(self, *, problem_memory, evidence, previous_refutations):
        context = FalseReviewRuntime._clean_problem_memory(problem_memory)
        try:
            target = refutation_target(str(context['formal_statement']))
        except ValueError as exc:
            return self.verify_claim(claim={"why": str(exc), "change": "", "unclear": True,
                                           "lean_proof": ""}, target="")
        inputs = {"problem_memory": context, **evidence, "refutation_target": target,
                  "previous_refutations": previous_refutations or []}
        gate = self.assess_round(inputs)
        if gate['action'] != 'refute':
            return {"decision": "next_round", "why": gate['rationale'],
                    "orchestrator_assessment": gate,
                    "claim": {"why": gate['rationale'], "change": "", "unclear": True,
                              "lean_proof": ""},
                    "refutation_target": target,
                    "verifier_result": {"success": False, "returncode": None,
                                        "timed_out": False, "formal_state": "",
                                        "lean_error_log": "Orchestrator chose continued exploration; no refutation attempted."}}
        inputs['orchestrator_assessment'] = gate
        prompt = BuiltPrompt(name="false_reason", version="v4.3-orchestrator-gated-refutation", inputs=inputs,
            text="""System:
You are the False Reasoner. Use the full problem and attempt memories to
construct a Lean proof of the FIXED refutation target. The orchestrator assessment
is an unverified argument to check, never an added Lean assumption. Truth memory contains
proof attempts; falsifier/counter_reasoning memory contains criticism, not
verified facts. Failed proof attempts do not establish falsity.
The target is constructed deterministically and is the correct negation; do not
second-guess or add another negation. If the original conclusion is ¬Q, the
refutation goal is ¬¬Q: prove Q and contradict the assumed ¬Q. For example,
for goal ¬ (¬ ((2 : ℕ) + 2 = 4)), use `intro h; apply h; norm_num`.
For a CLOSED numerical equality, use direct norm_num rather than long
factorizations or simp lists. Prefer `intro h; apply h; norm_num` for a
negated closed equality. Keep code minimal and all parentheses balanced.
For goal ¬ (∀ (x : T), H x → C x), use `intro h`, instantiate h with a
concrete witness and a proof of H, then derive a contradiction from C.
The target negates the WHOLE universally quantified original theorem. A
counterexample must satisfy ALL original hypotheses and violate its conclusion.
Do not assume the original conclusion is false, introduce extra assumptions,
change the target, or merely prove a corrected answer. If unable to refute it,
return an empty lean_proof and unclear=true. A failed refutation is not truth.
Return JSON only: {"why": "diagnosis", "change": "optional proposed repair",
"unclear": false, "lean_proof": "Lean tactic proof body"}.
lean_proof must contain only the tactic body after := by, with no theorem,
imports, commands, sorry, admit, axioms, native_decide.
The supplied verifier prelude provides the common dataset imports. Do not
run tools or shell commands. Use previous refutation errors to avoid repeats.
IMPORTANT: For a fully closed numerical target (including double negation),
your FIRST candidate must be just `norm_num`. Do not add factorization lemmas,
subst, or simp lists unless a previous verifier error shows norm_num failed.
Use subst only to eliminate a variable, never an equality between closed terms.

User:
""" + json.dumps(inputs, ensure_ascii=False, indent=2)
                  + "\nVerifier prelude:\n" + self.verifier.prelude)
        try:
            raw = self.model_client.complete(prompt)
            parsed = FalseReviewRuntime._parse_json(raw)
            claim = {"why": str(parsed.get('why', '')), "change": str(parsed.get('change', '')),
                     "unclear": parsed.get('unclear') is not False,
                     "lean_proof": str(parsed.get('lean_proof', ''))}
        except (ModelClientError, ValueError, TypeError) as exc:
            claim = {"why": f"Refutation generation failed: {exc}", "change": "",
                     "unclear": True, "lean_proof": ""}
        result = self.verify_claim(claim=claim, target=target)
        result['orchestrator_assessment'] = gate
        return result

    def assess_round(self, inputs: dict[str, Any]) -> dict[str, str]:
        prompt = BuiltPrompt(name="orchestrator.false_decision",
            version="v5-round-refutation-gate", inputs=inputs,
            text="""System:
You are the Orchestrator reviewing a completed search round. Read the original
problem and accumulated memories. Select exactly one action:
continue: give a new mathematical strategy for the next proof search round;
refute: judge that the statement itself is false, explain the exact mathematical
flaw and a concrete counterexample or other rigorous refutation argument, and
request the False Reasoner to formalize that argument in Lean.
Proof failures, unknown lemma names and verifier errors alone are not evidence
of falsity. For a counterexample check every hypothesis. A refute action is only
a proposal; final falsity requires a verified proof of the whole negation.
Do not use tools, inspect files, or assume any hidden label or mutation key.
field_simp is permitted for clearing denominators; handle its side conditions.
Return JSON only: {"action":"continue" or "refute", "rationale":"explanation",
"refutation_argument":"specific argument if refute, otherwise empty",
"next_strategy":"new proof search strategy if continue, otherwise empty"}.

User:
""" + json.dumps(inputs, ensure_ascii=False, indent=2))
        try:
            parsed = FalseReviewRuntime._parse_json(self.model_client.complete(prompt))
            argument = str(parsed.get('refutation_argument', '') or '').strip()
            rationale = str(parsed.get('rationale', '') or '').strip()
            action = 'refute' if parsed.get('action') == 'refute' and argument and rationale else 'continue'
            return {'action': action, 'rationale': rationale or 'No supported falsity judgment.',
                    'refutation_argument': argument,
                    'next_strategy': str(parsed.get('next_strategy', '') or '').strip()}
        except (ModelClientError, ValueError, TypeError) as exc:
            return {'action': 'continue', 'rationale': f'Orchestrator assessment failed: {exc}',
                    'refutation_argument': '', 'next_strategy': 'Review prior failures and change proof strategy.'}

    def verify_claim(self, *, claim: dict[str, Any], target: str) -> dict[str, Any]:
        body, _ = normalize_lean_proof(str(claim.get('lean_proof', '')))
        rejected = re.search(r"\b(sorry|admit|axiom|opaque|unsafe|constant|native_decide|"
                             r"run_tac|run_elab|run_meta|elab|macro)\b|(?m:^\s*(?:import|open|theorem|lemma|"
                             r"namespace|section|end|set_option|def|abbrev|instance|#)\b)", _without_lean_comments(body))
        if not target or not body or rejected:
            failure = (f"Unsupported refutation target: {claim.get('why', '')}" if not target
                       else "False Reasoner did not generate a refutation proof body." if not body
                       else f"Prohibited construct in refutation body: {rejected.group(0)}")
            result = {"success": False, "formal_state": "", "lean_error_log": failure,
                      "returncode": None, "timed_out": False}
        else:
            name = target.split()[1]
            # Without this option, Lean can silently introduce free identifiers
            # as OUTER implicit parameters, invalidating the universal closure.
            source = 'set_option autoImplicit false\n' + target + '\n' + textwrap.indent(body, '  ')
            # Check the actual proof's axiom dependencies, not just text tokens.
            source += f"\n\n#print axioms {name}\n"
            try:
                result = self.verifier.verify(source, formal_statement=target)
            except OSError as exc:
                result = {"success": False, "formal_state": source,
                          "lean_error_log": f"Lean verifier execution failed: {exc}",
                          "returncode": None, "timed_out": False}
            if result.get('success'):
                log = result.get('lean_error_log', '')
                # Read the final report for this exact declaration. Earlier
                # diagnostics or generated `trace` text cannot certify it.
                reports = list(re.finditer(
                    rf"'{re.escape(name)}'\s+(?:does not depend on any axioms|"
                    r"depends on axioms:\s*\[([^\]]*)\])", log, re.S))
                allowed = {'propext', 'Classical.choice', 'Quot.sound'}
                clean = bool(reports)
                if reports and reports[-1].group(1) is not None:
                    clean = set(filter(None, (s.strip() for s in reports[-1].group(1).split(',')))) <= allowed
                if not clean:
                    result = {**result, 'success': False, 'lean_error_log': log +
                              '\nRefutation axiom audit failed or dependency output was missing.'}
        success = bool(result.get('success'))
        return {"decision": "false" if success else "next_round",
                "why": "Lean verified the exact negation of the input theorem." if success
                       else str(result.get('lean_error_log', 'Refutation failed.')),
                "claim": claim, "refutation_target": target, "verifier_result": result}
