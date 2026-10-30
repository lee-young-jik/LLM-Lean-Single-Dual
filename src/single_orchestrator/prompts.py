from __future__ import annotations

from dataclasses import dataclass
import json
import re
from string import Formatter
from typing import Any, Mapping


OPTIONAL_PROMPT_FIELDS = {"previous_trial", "lean_context"}


MATHLIB_NAMING_DISCIPLINE = (
    "Mathlib naming discipline: do not invent declaration names. "
    "In Lean/Mathlib, theorem and lemma names are usually snake_case; "
    "types, structures, classes, and predicates are usually UpperCamelCase; "
    "functions and data are usually lowerCamelCase. A theorem name usually "
    "mentions the declarations it is about in expression order, not as a "
    "free-form English sentence. Do not assume a declaration is in the "
    "`Complex` namespace merely because the theorem is about complex numbers. "
    "`open` and `open scoped` do not create declarations or justify using "
    "unknown names. If the exact Mathlib name is uncertain, do not use it; "
    "prefer hypotheses already in context, basic tactics, or smaller proof "
    "steps. Never use names previously reported by Lean as `Unknown identifier` "
    "or `Unknown constant`."
)

MATHLIB_IMPORT_DISCIPLINE = (
    "Import discipline: the submitted Lean source must include any imports it "
    "needs and avoid broad `import Mathlib`. For miniF2F tasks, the evaluator may "
    "provide the common dataset prelude internally (`MiniF2F.ProblemImports` plus "
    "`open scoped Real/Nat/Topology/Polynomial`); when that prelude is shown in "
    "the Lean header, do not output any `import` or `open` lines at all; focus only "
    "on the proof body after `:= by`. Do not use broad imports `import Mathlib`, `import MiniF2F.Test`, or "
    "`import MiniF2F.Valid`; they are too expensive for per-trial verification and "
    "will be rejected. "
    "Use narrow imports only when needed, and choose module names that exist "
    "in the current Mathlib snapshot. Do not use broad tactic bundles such as "
    "`Mathlib.Tactic`; import the specific tactic modules you need instead. "
    "If Lean reports a bad or unavailable import, change only the import path "
    "or avoid that import on the next retry. "
    "If a Lean header "
    "contains a broad import, use it as context but replace that broad import "
    "with narrower imports; preserve useful `open` and `open scoped` lines."
)

LEAN_GENERATION_CONCISION = (
    "Generation discipline: return a short proof quickly. For concrete finite "
    "arithmetic, numerals, modular arithmetic, decidable equalities, or finite "
    "Finset/cardinality computations, first try the smallest direct proof body "
    "such as `by decide`, `by native_decide`, or `by norm_num` when appropriate. "
    "Do not use `decide` or `native_decide` for targets with free variables or "
    "universal quantifiers over non-finite domains unless those variables have "
    "already been instantiated to concrete finite values. "
    "Do not run shell commands, `lake`, `lean`, grep/search commands, or any "
    "local API exploration while generating the answer; respond from the prompt "
    "context only. "
    "Do not spend effort inventing a long general formalization when the target "
    "is a concrete computation. If the exact library route is uncertain, output "
    "the simplest plausible complete proof rather than reasoning indefinitely."
)

HEAVY_TACTIC_DISCIPLINE = (
    "Arithmetic tactic discipline: `field_simp` is permitted for clearing denominators. "
    "Keep denominator side conditions explicit. `nlinarith` is allowed only when it "
    "is likely to close a small local arithmetic goal quickly. If the previous "
    "failure says a timed-out Lean proof used `nlinarith`, the next retry must "
    "avoid `nlinarith` and use smaller local steps, rewriting, normalization, "
    "direct case splits, or simpler facts instead. If the previous failure says "
    "a timed-out Lean proof used `linarith`, the next retry should also avoid "
    "broad `linarith`; use explicit rewrites, `norm_num` for tiny numeric facts, "
    "`ring`/`ring_nf` for algebraic normalization, or direct positivity facts. "
    "Avoid broad `norm_num at h ⊢` on hypotheses or goals containing variables; "
    "it can spend the full verifier timeout. Instead, normalize only a closed "
    "numeric side condition with `norm_num`, rewrite the variable expression with "
    "`ring`/`ring_nf`, or state the simplified equation as a small `have`. "
    "For `Real.log` or logarithm proofs, do not use `linarith` or `nlinarith` "
    "anywhere in the submitted Lean code; the verifier rejects that combination "
    "before running Lean because it repeatedly causes MiniF2F timeouts. First "
    "convert every logarithm hypothesis into explicit equalities, prove "
    "positivity/nonzero side conditions directly, rewrite the product-log "
    "identity, and close small arithmetic goals with direct facts, `norm_num`, "
    "`ring`, `ring_nf`, `omega`, or a named lemma instead. Do not use `Real.logb`; "
    "it is unavailable in this MiniF2F environment and is rejected before Lean "
    "runs. When variables are naturals coerced "
    "to reals, avoid ambiguous unannotated multiplication/order lemmas; state "
    "intermediate inequalities explicitly as `ℝ` facts before using them. If a "
    "rewrite involving division fails to find its pattern, do not retry the same "
    "rewrite sequence; use direct implications such as `(div_eq_iff h).mp` or "
    "`(eq_div_iff h).2` with the required nonzero fact."
)

TRUTH_LEAN_REPAIR_DISCIPLINE = (
    "Truth-lane repair discipline: this lane must keep the role assumption that "
    "the target statement is true. Do not discuss counterexamples, missing "
    "hypotheses, or falsity in the Lean output. When previous trial feedback is "
    "present, prioritize repairing the exact reported Lean failure over inventing "
    "a new mathematical route. If the previous proof timed out, make the next "
    "proof shorter and more local; remove the timeout-prone tactic or API named "
    "in the feedback, split the proof into closed `have` blocks, and use direct "
    "rewrites or small normalizations. If the previous proof used an unavailable "
    "API, unknown identifier, malformed indentation, or an open subgoal, fix that "
    "specific issue first and do not repeat the same construct."
)

LEAN_OUTPUT_REJECTION_DISCIPLINE = (
    "Automatic rejection rule for Lean output: the verifier scans the submitted "
    "source text before running Lean. A `Real.log`/logarithm proof containing `linarith` or "
    "`nlinarith` is also rejected before Lean runs, and unavailable APIs such "
    "as `Real.logb` are rejected before Lean runs. Do not repeat prohibited "
    "tokens, unavailable APIs, or rejected timeout-prone combinations from "
    "previous feedback in the Lean code, comments, or surrounding text. Return "
    "only Lean code that avoids the rejected token, API, or combination "
    "completely."
)

LEAN_FORMALIZATION_DISCIPLINE = (
    "Lean formalization discipline: do not leave any subgoals open. If Lean "
    "previously reported `unsolved goals`, make the next proof smaller and close "
    "each `have` block before using it. Do not use local variables introduced by "
    "`let`, such as `u` or `v`, as rewrite rules; `rw [u]` is invalid because a "
    "local value is not an equality or definition theorem. Prefer `change`, "
    "`simp [u, v]`, or explicit `have` equalities when unfolding local lets. "
    "Never reuse names that Lean reported as `Unknown identifier` or `Unknown "
    "constant`; replace them with hypotheses already in context or basic local "
    "proofs. If a rewrite failed because Lean could not find the pattern, do not "
    "repeat the same `rw`; first restate the expression into a matching form with "
    "a small `have`, `change`, or `calc` step. If Lean reports `No goals to be "
    "solved`, the proof already closed before a trailing tactic; remove the "
    "extra cleanup tactic and make the closing tactic the final line. Do not add "
    "a final `linarith`, `norm_num`, `ring`, `simp`, or `exact` after an `apply`, "
    "`exact`, `calc`, or local arithmetic step has already closed the goal. "
    "For finite digit or bounded natural-number case splits with `interval_cases`, "
    "prefer `interval_cases n <;> simp_all` over `interval_cases n <;> norm_num "
    "at h ⊢`; the latter can fail with `No goals to be solved` when some branches "
    "are already closed before the target simplification runs."
)


@dataclass(frozen=True)
class PromptSpec:
    name: str
    version: str
    system: str
    template: str
    input_fields: tuple[str, ...]

    @property
    def required_fields(self) -> set[str]:
        return {
            field_name
            for _, field_name, _, _ in Formatter().parse(self.template)
            if field_name and field_name not in OPTIONAL_PROMPT_FIELDS
        }


@dataclass(frozen=True)
class BuiltPrompt:
    name: str
    version: str
    inputs: dict[str, Any]
    text: str


class PromptRegistry:
    def __init__(self, specs: Mapping[str, PromptSpec]):
        self._specs = dict(specs)

    @classmethod
    def defaults(cls) -> "PromptRegistry":
        return cls(
            {
                "orchestrator.strategy": PromptSpec(
                    name="orchestrator.strategy",
                    version="v3",
                    system=(
                        "You are the single orchestrator for Lean proof generation. "
                        "Choose a mathematical strategy for solving the problem. Do not "
                        "write Lean code. Do not mention tactics, imports, theorem names, "
                        "or Mathlib APIs. "
                        f"{HEAVY_TACTIC_DISCIPLINE}"
                    ),
                    input_fields=("problem", "lean_header"),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Lean header:\n{lean_header}\n\n"
                        "Return a concise mathematical strategy for the Nature agent."
                    ),
                ),
                "nature_agent.initial": PromptSpec(
                    name="nature_agent.initial",
                    version="v2",
                    system=(
                        "You are the Nature agent. Write a human-style natural-language "
                        "mathematical proof. Do not describe how to formalize the proof; "
                        "write the proof as a mathematician would. Do not write Lean code, "
                        "tactics, imports, theorem names, Mathlib APIs, or formalization "
                        "strategy. "
                        f"{HEAVY_TACTIC_DISCIPLINE}"
                    ),
                    input_fields=("problem", "strategy"),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Orchestrator strategy:\n{strategy}\n\n"
                        "Return only the natural-language mathematical proof. Make the "
                        "argument logically complete for a human reader. State substitutions, "
                        "identities, inequalities, and case splits explicitly."
                    ),
                ),
                "nature_agent.retry": PromptSpec(
                    name="nature_agent.retry",
                    version="v2",
                    system=(
                        "You are the Nature agent. Revise the human-style natural-language "
                        "mathematical proof using the previous trial memory. Do not describe "
                        "how to formalize the proof; write the proof as a mathematician "
                        "would. Do not write Lean code, tactics, imports, theorem names, "
                        "Mathlib APIs, or formalization strategy. "
                        f"{HEAVY_TACTIC_DISCIPLINE}"
                    ),
                    input_fields=("problem", "strategy", "previous_trial"),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Orchestrator strategy:\n{strategy}\n\n"
                        "Previous trial memory, if any:\n{previous_trial}\n\n"
                        "Return only the revised natural-language mathematical proof. Make "
                        "the mathematical correction explicit."
                    ),
                ),
                "lean_generator.generate": PromptSpec(
                    name="lean_generator.generate",
                    version="v4",
                    system=(
                        "You generate Lean 4 code from the problem and the informal proof. "
                        "You are responsible for formalization choices; the Nature agent's "
                        "output is only a human mathematical proof. "
                        "Previous trial feedback may include Lean errors and retry guidance. "
                        "Use it to avoid repeating failed imports, unavailable APIs, or broken "
                        "proof structure. If a target Lean statement is "
                        "provided, prove exactly that statement and do not add assumptions or "
                        "change its quantifiers. The target statement is a proof skeleton, not "
                        "a proof body. If a Lean header is provided, use it as context for "
                        "imports and opened namespaces. If allowed imports are provided, stay "
                        "within those libraries. If no allowed imports are provided, use the narrowest "
                        "necessary Mathlib imports you can identify; avoid broad `import "
                        "Mathlib` unless no specific import is reasonably known. Never use "
                        "`sorry`, `admit`, `axiom`, or any "
                        "placeholder declaration. Do not introduce new axioms or theorem "
                        "statements to stand in for missing library facts. "
                        f"{HEAVY_TACTIC_DISCIPLINE} "
                        f"{LEAN_OUTPUT_REJECTION_DISCIPLINE} "
                        f"{LEAN_GENERATION_CONCISION} "
                        f"{MATHLIB_IMPORT_DISCIPLINE} "
                        f"{MATHLIB_NAMING_DISCIPLINE} "
                        f"{LEAN_FORMALIZATION_DISCIPLINE}"
                    ),
                    input_fields=(
                        "problem",
                        "current_informal",
                        "formal_statement",
                        "allowed_imports",
                        "lean_header",
                        "lean_context",
                        "previous_trial",
                    ),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Informal proof:\n{current_informal}\n\n"
                        "Previous trial feedback, if any:\n{previous_trial}\n\n"
                        "Target Lean statement skeleton, if provided:\n{formal_statement}\n\n"
                        "Allowed imports, if provided:\n{allowed_imports}\n\n"
                        "Lean header, if provided:\n{lean_header}\n\n"
                        "Lean environment guardrails, if provided:\n{lean_context}\n\n"
                        "Return Lean 4 code only. Prefer a single fenced lean block if needed. "
                        "When a target statement skeleton is provided, keep the theorem "
                        "statement exactly aligned with it and fill only the proof body. "
                        "If the Lean header provides a miniF2F common prelude, do not repeat "
                        "that prelude and do not output any `import` or `open` lines; return "
                        "only the proof body or the target theorem with its body. If a Lean "
                        "header is provided, use useful namespace/scoped lines "
                        "as context, but prefer narrow imports over a broad `import Mathlib`. "
                        "If allowed imports are listed, stay within those libraries. "
                        "If imports are needed and no allowed imports are listed, prefer "
                        "specific Mathlib modules over `import Mathlib`. The submitted code "
                        "must be a complete proof. "
                        "Do not use `sorry`, `admit`, `axiom`, `opaque`, or invented helper "
                        "theorems without proofs. If a library theorem name is uncertain, "
                        "try a proof from available facts instead of declaring an axiom. "
                        f"{HEAVY_TACTIC_DISCIPLINE} "
                        f"{LEAN_OUTPUT_REJECTION_DISCIPLINE} "
                        f"{LEAN_GENERATION_CONCISION} "
                        f"{MATHLIB_IMPORT_DISCIPLINE} "
                        f"{MATHLIB_NAMING_DISCIPLINE} "
                        f"{LEAN_FORMALIZATION_DISCIPLINE}"
                    ),
                ),
                "retry_planner.plan": PromptSpec(
                    name="retry_planner.plan",
                    version="v3",
                    system=(
                        "You are the retry planner for the informal proof loop. "
                        "Explain what failed and what the Nature agent should change in the "
                        "next human-style proof. Do not write Lean code. Do not suggest "
                        "tactics, imports, or Mathlib API details unless the failure is purely "
                        "a missing formalization route. If the failure is an import failure, "
                        "say that the mathematical proof can stay the same and the next Lean "
                        "attempt must choose existing narrow imports in the local snapshot, "
                        "or avoid unnecessary imports. If the failure mentions prohibited "
                        "placeholders, the next plan must explicitly avoid `sorry`, `admit`, "
                        "`axiom`, and invented unproved lemmas."
                        f" {HEAVY_TACTIC_DISCIPLINE}"
                    ),
                    input_fields=(
                        "problem",
                        "failure_context",
                    ),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Failure context:\n{failure_context}\n\n"
                        "Return a short retry plan for the next informal proof. "
                        "Translate syntax/API failures into mathematical clarification "
                        "requests where possible. Do not write Lean code. Do not recommend "
                        "axioms, placeholders, or assuming unproved library facts. If the "
                        "failure reason is an unavailable or overly broad import, keep the "
                        "mathematical strategy stable and instruct the next Lean attempt to "
                        "use existing narrow imports from the local project snapshot. "
                        f"{HEAVY_TACTIC_DISCIPLINE}"
                    ),
                ),
                "orchestrator.restrategy": PromptSpec(
                    name="orchestrator.restrategy",
                    version="v3",
                    system=(
                        "You are the single orchestrator. The current strategy round failed. "
                        "Use the compact trial memory to propose a new strategy for the next "
                        "Nature agent round. Choose whether to keep the same mathematical "
                        "approach or switch strategy. Do not write Lean code. Do not mention "
                        "tactics, imports, theorem names, or Mathlib APIs. "
                        f"{HEAVY_TACTIC_DISCIPLINE}"
                    ),
                    input_fields=("problem", "lean_header", "strategy", "trial_memory"),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Lean header:\n{lean_header}\n\n"
                        "Original strategy:\n{strategy}\n\n"
                        "Trial memory:\n{trial_memory}\n\n"
                        "Return a revised mathematical strategy for the Nature agent."
                    ),
                ),
                "orchestrator.false_decision": PromptSpec(
                    name="orchestrator.false_decision",
                    version="v7",
                    system=(
                        "You are the single orchestrator false-decision checker. "
                        "The Target Lean statement shown in the prompt is complete: all "
                        "mathematical hypotheses are explicit there, and there are no hidden "
                        "premises elsewhere in the environment. Imports and headers provide "
                        "definitions and notation only, not additional assumptions. "
                        "Your job is only to decide whether the exact target should stop "
                        "as false after a failed three-trial strategy round. Be extremely "
                        "conservative: failed proof attempts, timeouts, missing APIs, weak "
                        "informal suspicion, or repeated Lean errors are not enough. Read "
                        "the accumulated trial memory and choose false only when you can "
                        "state a clear mathematical refutation of the exact target from that "
                        "memory: a concrete counterexample, a proof of the exact negation, "
                        "or an explicit contradiction in the target itself such as a required "
                        "`False` conjunct under satisfiable hypotheses. The refutation may be "
                        "mathematical rather than Lean-verified, but it must be specific, "
                        "checkable, and independent of mere proof failure. Do not use these "
                        "false-evidence categories for ordinary Lean failures or hard missing "
                        "proofs. Counterexample evidence may appear in the informal proof, "
                        "formal attempt, Lean error log, retry plan, or comments generated by "
                        "the proof agent; if it states a concrete assignment and a checkable "
                        "contradiction to the exact formal target, treat it as usable "
                        "clear_counterexample evidence. If you "
                        "are confused, uncertain, or the evidence is ambiguous or incomplete, "
                        "do not choose false; choose continue so the orchestrator can "
                        "re-strategize. If the informal problem text and Lean formal statement "
                        "disagree, judge the exact Lean target shown in the prompt; a concrete "
                        "counterexample to that exact target is valid false evidence even when "
                        "the original informal problem text would be true. A false decision "
                        "must clearly explain why the exact target is false, cite "
                        "the evidence from memory, and state the minimal mathematical "
                        "change that would make the exact target true. Before choosing false, "
                        "independently check every hypothesis for the proposed witness and "
                        "check that the conclusion is actually falsified. Any unverified "
                        "side condition, approximate numerical claim, missing witness value, "
                        "or unresolved doubt requires continue, never false. Return JSON only."
                    ),
                    input_fields=(
                        "problem",
                        "formal_statement",
                        "lean_header",
                        "strategy",
                        "trial_memory",
                    ),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Target Lean statement:\n{formal_statement}\n\n"
                        "Lean header:\n{lean_header}\n\n"
                        "Current strategy:\n{strategy}\n\n"
                        "Three-trial memory for this strategy round:\n{trial_memory}\n\n"
                        "Return JSON with keys decision, rationale, false_evidence, "
                        "correction_to_true, validation_complete, hypotheses_verified, "
                        "conclusion_falsified, uncertainties. "
                        "decision must be one of false, continue, unknown. "
                        "false_evidence must be one of none, validated_counterexample, "
                        "validated_negation, clear_counterexample, clear_negation, "
                        "explicit_contradiction. These are output categories you assign "
                        "after reading the memory; do not expect these category names to "
                        "appear literally in the memory. Use none unless decision is false. Use "
                        "clear_counterexample only when the memory supports a concrete "
                        "assignment or instance falsifying the exact target. Use clear_negation "
                        "only when the memory supports a direct mathematical proof of the "
                        "exact negation. Use explicit_contradiction only when the exact formal target "
                        "itself requires an impossible component such as `False` and the "
                        "rationale gives concrete satisfiable hypotheses or an instance showing "
                        "the contradiction. If decision "
                        "is false, rationale must explicitly state the concrete reason the "
                        "exact target is false, not merely that proof attempts failed. "
                        "If decision is false, correction_to_true must state the exact "
                        "mathematical edit that would make the Lean target true, for example "
                        "`change RHS from 1/4 to 1/2`, `replace < with <=`, or "
                        "`add hypothesis n ≠ 64`. If "
                        "anything is even slightly unclear, choose continue so the next "
                        "step is re-strategy, never false. For false, all three validation "
                        "booleans must be true and uncertainties must be an empty array."
                    ),
                ),
                "false_judgment_agent.assess": PromptSpec(
                    name="false_judgment_agent.assess",
                    version="v3",
                    system=(
                        "You are a small falsity judgment agent. "
                        "Read the accumulated proof memory and the exact Lean target. "
                        "The target shown is the complete proposition: do not speculate that "
                        "missing hypotheses may exist elsewhere in the environment. "
                        "Your job is to make a preliminary false/continue judgment and, "
                        "if false, say what mathematical edit would make the exact target "
                        "true. Do not prove the theorem. If the memory does not clearly "
                        "support falsity and a specific correction, choose continue and "
                        "return an empty correction_to_true. A proposed refutation must "
                        "explicitly check all hypotheses and the failed conclusion; list "
                        "every unresolved issue in uncertainties. Return JSON only."
                    ),
                    input_fields=(
                        "problem",
                        "formal_statement",
                        "evidence_memory",
                    ),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Target Lean statement:\n{formal_statement}\n\n"
                        "Accumulated memory:\n{evidence_memory}\n\n"
                        "Return JSON with keys decision, rationale, false_evidence, "
                        "correction_to_true, hypotheses_check, conclusion_check, "
                        "uncertainties. decision must be one of false, continue, "
                        "unknown. false_evidence must be one of none, "
                        "clear_counterexample, clear_negation, explicit_contradiction, "
                        "wrong_constant, relation_flip, strictness_error, missing_hypothesis. "
                        "correction_to_true must be the minimal edit needed to make the "
                        "exact Lean target true, for example `change RHS from 1/4 to 1/2`, "
                        "`replace t != 4 with t = 4`, or `replace strict < with <=`. "
                        "If unclear, use false_evidence `none`, correction_to_true ``, and "
                        "put the ambiguity in uncertainties."
                    ),
                ),
                "orchestrator.false_judgment_review": PromptSpec(
                    name="orchestrator.false_judgment_review",
                    version="v1",
                    system=(
                        "You are the orchestrator reviewing a preliminary false judgment "
                        "from a smaller model against the hidden dataset transform key. "
                        "Do not generate a new falsity argument from scratch. Compare the "
                        "smaller model's proposed correction_to_true to the recorded "
                        "correction for this deliberately false example. Approve false only "
                        "when they identify the same mathematical edit. Otherwise choose "
                        "continue. Return JSON only."
                    ),
                    input_fields=(
                        "problem",
                        "formal_statement",
                        "evidence_memory",
                        "false_agent_result",
                        "expected_correction",
                    ),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Target Lean statement:\n{formal_statement}\n\n"
                        "Accumulated memory:\n{evidence_memory}\n\n"
                        "Smaller-model false judgment:\n{false_agent_result}\n\n"
                        "Recorded dataset correction:\n{expected_correction}\n\n"
                        "Return JSON with keys decision, rationale, false_evidence, "
                        "correction_to_true. decision must be false, continue, or unknown. "
                        "If approving false, reuse the smaller model's correction_to_true "
                        "only when it is equivalent to the recorded dataset correction. If "
                        "not approving, choose continue and explain the mismatch."
                    ),
                ),
                "dual_orchestrator.truth_strategy": PromptSpec(
                    name="dual_orchestrator.truth_strategy",
                    version="v2",
                    system=(
                        "You are the dual orchestrator. Create the truth-side strategy. "
                        "This side is the pro-theorem side: it must assume the theorem is "
                        "true as a role assignment, defend it, and choose a mathematical "
                        "route for proving it. The truth side must not evaluate whether the "
                        "statement is actually true; it must act as an advocate for truth. "
                        "Do not hedge that the statement may be false. Do not search for "
                        "counterexamples. Do not write Lean code. Do not mention tactics, "
                        "imports, theorem names, or Mathlib APIs. "
                        f"{HEAVY_TACTIC_DISCIPLINE}"
                    ),
                    input_fields=(
                        "problem",
                        "formal_statement",
                        "lean_header",
                        "round_summaries",
                    ),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Target Lean statement:\n{formal_statement}\n\n"
                        "Lean header:\n{lean_header}\n\n"
                        "Prior round summaries, if any:\n{round_summaries}\n\n"
                        "Return a concise truth-side mathematical strategy."
                    ),
                ),
                "dual_orchestrator.falsity_strategy": PromptSpec(
                    name="dual_orchestrator.falsity_strategy",
                    version="v2",
                    system=(
                        "You are the dual orchestrator. Create the falsity-side strategy. "
                        "This side is the anti-theorem side: it must assume the theorem is "
                        "false as a role assignment and attack it as strongly as possible. "
                        "The falsity side must not evaluate whether the statement is "
                        "actually true and must never conclude that the theorem is true, "
                        "probably true, or has no counterexample. If one attack fails, "
                        "discard it and propose the next strongest attack. Look for a "
                        "concrete counterexample, a missing hypothesis, a hidden domain "
                        "issue, an edge case, a type/definition mismatch, or a contradiction "
                        "in the exact statement. Do not defend the theorem. Do not write "
                        "Lean code. Do not mention tactics, imports, theorem names, or "
                        "Mathlib APIs. "
                        f"{HEAVY_TACTIC_DISCIPLINE}"
                    ),
                    input_fields=(
                        "problem",
                        "formal_statement",
                        "lean_header",
                        "round_summaries",
                    ),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Target Lean statement:\n{formal_statement}\n\n"
                        "Lean header:\n{lean_header}\n\n"
                        "Prior round summaries, if any:\n{round_summaries}\n\n"
                        "Return a concise falsity-side mathematical strategy."
                    ),
                ),
                "truth_nature_agent.initial": PromptSpec(
                    name="truth_nature_agent.initial",
                    version="v1",
                    system=(
                        "You are the truth-side Nature agent. Write a human-style "
                        "natural-language proof from the pro-theorem position. You must "
                        "assume the statement is true and try to prove it like a human "
                        "mathematician. Do not discuss falsity, counterexamples, or missing "
                        "hypotheses. Do not write Lean code, tactics, imports, theorem "
                        "names, Mathlib APIs, or formalization strategy. "
                        f"{HEAVY_TACTIC_DISCIPLINE}"
                    ),
                    input_fields=("problem", "strategy"),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Truth-side strategy:\n{strategy}\n\n"
                        "Return only the natural-language mathematical proof."
                    ),
                ),
                "truth_nature_agent.retry": PromptSpec(
                    name="truth_nature_agent.retry",
                    version="v1",
                    system=(
                        "You are the truth-side Nature agent. Revise the human-style proof "
                        "using only the previous error feedback while staying in the "
                        "pro-theorem role. Keep assuming the statement is true and repair "
                        "the proof route. Do not switch to a falsity analysis. Do not write "
                        "Lean code, tactics, imports, theorem names, Mathlib APIs, or "
                        "formalization strategy. "
                        f"{HEAVY_TACTIC_DISCIPLINE}"
                    ),
                    input_fields=("problem", "strategy", "previous_trial"),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Truth-side strategy:\n{strategy}\n\n"
                        "Previous trial feedback:\n{previous_trial}\n\n"
                        "Return only the revised natural-language proof."
                    ),
                ),
                "falsity_nature_agent.initial": PromptSpec(
                    name="falsity_nature_agent.initial",
                    version="v2",
                    system=(
                        "You are the falsity-side Nature agent. You must take the "
                        "anti-theorem position and assume the statement is false as a role "
                        "assignment. Write a human-style attack: a candidate counterexample, "
                        "a missing hypothesis, a hidden domain issue, an edge case, a "
                        "type/definition mismatch, or a contradiction in the exact "
                        "statement. Push the falsity case as strongly as possible. Never "
                        "conclude that the statement is true, probably true, or that no "
                        "counterexample exists. If a candidate attack fails, move to the "
                        "next strongest attack instead of defending the theorem. Do not "
                        "write Lean code, tactics, imports, theorem names, or Mathlib APIs. "
                        f"{HEAVY_TACTIC_DISCIPLINE}"
                    ),
                    input_fields=("problem", "strategy"),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Falsity-side strategy:\n{strategy}\n\n"
                        "Return only the natural-language falsity analysis."
                    ),
                ),
                "falsity_nature_agent.retry": PromptSpec(
                    name="falsity_nature_agent.retry",
                    version="v2",
                    system=(
                        "You are the falsity-side Nature agent. Revise the counterargument "
                        "using only the previous error feedback while staying in the "
                        "anti-theorem role. Keep assuming the statement is false and repair "
                        "the attack. Never conclude that the theorem is true, probably true, "
                        "or has no counterexample. If a specific attack is blocked, replace "
                        "it with a stronger or more precise falsity attack. Do not write "
                        "Lean code, tactics, imports, theorem names, or Mathlib APIs. "
                        f"{HEAVY_TACTIC_DISCIPLINE}"
                    ),
                    input_fields=("problem", "strategy", "previous_trial"),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Falsity-side strategy:\n{strategy}\n\n"
                        "Previous trial feedback:\n{previous_trial}\n\n"
                        "Return only the revised natural-language falsity analysis."
                    ),
                ),
                "truth_lean_generator.generate": PromptSpec(
                    name="truth_lean_generator.generate",
                    version="v4",
                    system=(
                        "You generate Lean 4 code for the truth-side proof. Prove exactly "
                        "the target statement when provided. Return raw Lean code only. Do "
                        "not use `sorry`, `admit`, `axiom`, `opaque`, or unproved helper "
                        "declarations. If a Lean header is provided, use it as context for "
                        "imports and opened namespaces. If allowed imports are provided, stay "
                        "within those libraries. "
                        f"{TRUTH_LEAN_REPAIR_DISCIPLINE} "
                        f"{HEAVY_TACTIC_DISCIPLINE} "
                        f"{LEAN_GENERATION_CONCISION} "
                        f"{MATHLIB_IMPORT_DISCIPLINE} "
                        f"{LEAN_OUTPUT_REJECTION_DISCIPLINE} "
                        f"{MATHLIB_NAMING_DISCIPLINE} "
                        f"{LEAN_FORMALIZATION_DISCIPLINE}"
                    ),
                    input_fields=(
                        "problem",
                        "current_informal",
                        "formal_statement",
                        "allowed_imports",
                        "lean_header",
                        "previous_trial",
                    ),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Truth-side informal proof:\n{current_informal}\n\n"
                        "Previous trial feedback, if any:\n{previous_trial}\n\n"
                        "Target Lean statement skeleton:\n{formal_statement}\n\n"
                        "Allowed imports, if provided:\n{allowed_imports}\n\n"
                        "Lean header, if provided:\n{lean_header}\n\n"
                        "Return complete Lean 4 source code only. Prefer narrow imports over "
                        "a broad `import Mathlib` when you know the needed modules. "
                        f"{TRUTH_LEAN_REPAIR_DISCIPLINE} "
                        f"{HEAVY_TACTIC_DISCIPLINE} "
                        f"{LEAN_GENERATION_CONCISION} "
                        f"{MATHLIB_IMPORT_DISCIPLINE} "
                        f"{LEAN_OUTPUT_REJECTION_DISCIPLINE} "
                        f"{MATHLIB_NAMING_DISCIPLINE} "
                        f"{LEAN_FORMALIZATION_DISCIPLINE}"
                    ),
                ),
                "falsity_lean_generator.generate": PromptSpec(
                    name="falsity_lean_generator.generate",
                    version="v1",
                    system=(
                        "You generate Lean 4 code for the falsity-side attempt. Return raw "
                        "Lean code only. Do not use `sorry`, `admit`, `axiom`, `opaque`, "
                        "or unproved helper declarations. Previous trial "
                        "feedback may include Lean errors and retry guidance. Use it to "
                        "avoid repeating failed imports, unavailable APIs, or broken proof "
                        "structure. If a Lean header is provided, use it as context for "
                        "imports and opened namespaces. If allowed imports are provided, "
                        "stay within those libraries. "
                        f"{HEAVY_TACTIC_DISCIPLINE} "
                        f"{LEAN_GENERATION_CONCISION} "
                        f"{MATHLIB_IMPORT_DISCIPLINE} "
                        f"{LEAN_OUTPUT_REJECTION_DISCIPLINE} "
                        f"{MATHLIB_NAMING_DISCIPLINE} "
                        f"{LEAN_FORMALIZATION_DISCIPLINE}"
                    ),
                    input_fields=(
                        "problem",
                        "current_informal",
                        "formal_statement",
                        "allowed_imports",
                        "lean_header",
                        "previous_trial",
                    ),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Falsity-side informal argument:\n{current_informal}\n\n"
                        "Previous trial feedback, if any:\n{previous_trial}\n\n"
                        "Target Lean statement skeleton:\n{formal_statement}\n\n"
                        "Allowed imports, if provided:\n{allowed_imports}\n\n"
                        "Lean header, if provided:\n{lean_header}\n\n"
                        "Return complete Lean 4 source code only. Prefer narrow imports over "
                        "a broad `import Mathlib` when you know the needed modules. "
                        f"{HEAVY_TACTIC_DISCIPLINE} "
                        f"{LEAN_GENERATION_CONCISION} "
                        f"{MATHLIB_IMPORT_DISCIPLINE} "
                        f"{LEAN_OUTPUT_REJECTION_DISCIPLINE} "
                        f"{MATHLIB_NAMING_DISCIPLINE} "
                        f"{LEAN_FORMALIZATION_DISCIPLINE}"
                    ),
                ),
                "truth_retry_planner.plan": PromptSpec(
                    name="truth_retry_planner.plan",
                    version="v1",
                    system=(
                        "You are the truth-side retry planner. Explain what failed and what "
                        "the truth-side Nature agent should change next. Do not write Lean "
                        "code. Do not recommend placeholders. "
                        f"{HEAVY_TACTIC_DISCIPLINE}"
                    ),
                    input_fields=("problem", "failure_context"),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Failure context:\n{failure_context}\n\n"
                        "Return a short truth-side retry plan."
                    ),
                ),
                "dual_orchestrator.decision": PromptSpec(
                    name="dual_orchestrator.decision",
                    version="v6",
                    system=(
                        "You are the dual orchestrator decision maker for falsity only. "
                        "The Target Lean statement shown in the prompt is complete: all "
                        "mathematical hypotheses are explicit there, and there are no hidden "
                        "premises elsewhere in the environment. Imports and headers provide "
                        "definitions and notation only, not additional assumptions. "
                        "Do not decide that the theorem is true. Truth/solved is determined "
                        "outside this prompt only by a generated Lean proof passing lake "
                        "build and the judge. Your job is only to decide whether the problem "
                        "should stop as false or continue to another round. The two lanes "
                        "were generated under role assumptions: the truth lane was forced "
                        "to assume the target is true, and the falsity lane was forced to "
                        "assume the target is false. Treat both as adversarial working "
                        "hypotheses, not as facts. Be extremely "
                        "conservative about false: failed proof attempts, timeouts, missing "
                        "APIs, or weak informal suspicion are not enough. "
                        "Read the accumulated truth and falsity memories, but do not trust "
                        "either lane merely because of its role assumption. Only choose false "
                        "when the memories support a clear mathematical refutation of the "
                        "exact target: a concrete counterexample, a proof of the exact "
                        "negation, or an explicit contradiction in the target itself such as "
                        "a required `False` conjunct under satisfiable hypotheses. The "
                        "refutation may be mathematical rather than Lean-verified, but it "
                        "must be specific, checkable, and independent of mere proof failure. "
                        "Counterexample evidence may appear in either lane's informal proof, "
                        "formal attempt, Lean error log, retry plan, or comments generated by "
                        "the proof agent; if it states a concrete assignment and a checkable "
                        "contradiction to the exact formal target, treat it as usable "
                        "clear_counterexample evidence. If you "
                        "are confused, uncertain, or the evidence is ambiguous or incomplete, "
                        "do not choose false; choose continue so the orchestrator can "
                        "re-strategize. If the informal problem text and Lean formal statement "
                        "disagree, judge the exact Lean target shown in the prompt; a concrete "
                        "counterexample to that exact target is valid false evidence even when "
                        "the original informal problem text would be true. A false decision "
                        "must clearly explain why the exact target is false, cite "
                        "the evidence from memory, and state the minimal mathematical "
                        "change that would make the exact target true. Independently verify "
                        "every hypothesis and the negated conclusion. Any unverified side "
                        "condition, approximate calculation, or ambiguity requires continue. "
                        "Return JSON only."
                    ),
                    input_fields=(
                        "problem",
                        "formal_statement",
                        "lean_header",
                        "truth_memory",
                        "falsity_memory",
                    ),
                    template=(
                        "Problem:\n{problem}\n\n"
                        "Target Lean statement:\n{formal_statement}\n\n"
                        "Lean header:\n{lean_header}\n\n"
                        "Important: truth memory comes from a lane forced to assume true; "
                        "falsity memory comes from a lane forced to assume false. Do not "
                        "treat either role assumption as evidence by itself.\n\n"
                        "Truth memory:\n{truth_memory}\n\n"
                        "Falsity memory:\n{falsity_memory}\n\n"
                        "Return JSON with keys decision, rationale, false_evidence, "
                        "correction_to_true, validation_complete, hypotheses_verified, "
                        "conclusion_falsified, uncertainties. "
                        "decision must be one of false, continue, unknown. Never return true. "
                        "false_evidence must be one of none, validated_counterexample, "
                        "validated_negation, clear_counterexample, clear_negation, "
                        "explicit_contradiction. These are output categories you assign "
                        "after reading the memories; do not expect these category names to "
                        "appear literally in the memories. Use none unless decision is false. Use "
                        "clear_counterexample only when the memories support a concrete "
                        "assignment or instance falsifying the exact target. Use clear_negation "
                        "only when the memories support a direct mathematical proof of the "
                        "exact negation. Use explicit_contradiction only when the exact target "
                        "itself requires an impossible component such as `False` and the "
                        "rationale gives concrete satisfiable hypotheses or an instance showing "
                        "the contradiction. If decision "
                        "is false, rationale must explicitly state the concrete reason the "
                        "exact target is false, not merely that proof attempts failed. "
                        "If decision is false, correction_to_true must state the exact "
                        "mathematical edit that would make the Lean target true, for example "
                        "`change RHS from 1/4 to 1/2`, `replace < with <=`, or "
                        "`add hypothesis n ≠ 64`. If "
                        "anything is even slightly unclear, choose continue so the next "
                        "step is re-strategy, never false. For false, all three validation "
                        "booleans must be true and uncertainties must be an empty array."
                    ),
                ),
                "correction_judge.compare": PromptSpec(
                    name="correction_judge.compare",
                    version="v2",
                    system=(
                        "You are a strict correction-equivalence judge. Compare the "
                        "model's proposed correction against the hidden recorded correction "
                        "for a deliberately false Lean target. Decide whether the proposed "
                        "correction identifies the same mathematical edit needed to make "
                        "the target true. Accept equivalent wording, equivalent notation, "
                        "and more precise versions of the same correction. Reject vague "
                        "answers, different fixes, fixes that only mention proof failure, "
                        "or fixes that do not identify the changed mathematical content. "
                        "Also reject when the memory does not independently support the "
                        "refutation, even if the proposed edit happens to match the key. "
                        "Any ambiguity or incomplete check must be reported and rejected. "
                        "Return JSON only."
                    ),
                    input_fields=(
                        "formal_statement",
                        "evidence_memory",
                        "false_rationale",
                        "expected_correction",
                        "proposed_correction",
                    ),
                    template=(
                        "Target Lean statement:\n{formal_statement}\n\n"
                        "Evidence memory available to the orchestrator:\n{evidence_memory}\n\n"
                        "False-decision rationale:\n{false_rationale}\n\n"
                        "Recorded correction:\n{expected_correction}\n\n"
                        "Proposed correction:\n{proposed_correction}\n\n"
                        "Judge whether the proposed correction is both equivalent to the "
                        "recorded correction and supported by the evidence memory/rationale. "
                        "Return JSON with keys equivalent, evidence_supported, "
                        "uncertainties, and rationale. Both booleans must be true and "
                        "uncertainties must be empty for approval."
                    ),
                ),
            }
        )

    def build(self, name: str, **inputs: Any) -> BuiltPrompt:
        spec = self._specs[name]
        extras = set(inputs) - set(spec.input_fields)
        if extras:
            extra_list = ", ".join(sorted(extras))
            raise ValueError(f"Prompt {name} received non-whitelisted fields: {extra_list}")

        missing = spec.required_fields - set(inputs)
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise ValueError(f"Prompt {name} is missing fields: {missing_list}")

        render_inputs = {
            key: self._format_value(inputs.get(key, ""))
            for key in spec.input_fields
        }
        trace_inputs = {
            key: value
            for key, value in render_inputs.items()
            if value != ""
        }
        user_text = spec.template.format(**render_inputs)
        text = f"System:\n{spec.system}\n\nUser:\n{self._strip_empty_optional_sections(user_text)}"
        return BuiltPrompt(name=spec.name, version=spec.version, inputs=trace_inputs, text=text)

    def build_from_state(self, name: str, state: Mapping[str, Any]) -> BuiltPrompt:
        spec = self._specs[name]
        allowed = {field: state[field] for field in spec.input_fields if field in state}
        return self.build(name, **allowed)

    @staticmethod
    def _format_value(value: Any) -> str:
        if value in ((), [], {}):
            return ""
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return "" if value is None else str(value)

    @staticmethod
    def _strip_empty_optional_sections(text: str) -> str:
        optional_headers = (
            "Allowed imports, if provided",
            "Lean environment guardrails, if provided",
            "Lean header, if provided",
            "Lean header",
            "Prior round summaries, if any",
        )
        for header in optional_headers:
            text = re.sub(
                rf"\n\n{re.escape(header)}:\n[ \t\r\f\v]*\n",
                "\n\n",
                text,
            )
        return text
