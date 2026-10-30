"""Shared prompts for Single and Dual prove-or-disprove search."""

GOAL_MEANING = """Your only proof target is goal. In both tasks, prove the selected goal exactly as supplied. The task label identifies the direction; it does not ask you to change the target. For task=disprove, goal already expresses the required refutation: prove it, do not negate or refute it again. Do not prove the opposite of goal.

"""

PLANNER = """Prove the selected or supplied Lean goal exactly as written. If it already expresses a negation or refutation, prove that goal itself; do not negate or refute it again.
Read the supplied Lean goal or goals, any provided problem context, and all previous trial records.
Previous records are grouped by round and direction: one Lean strategy and an independently produced natural-language argument, followed by Lean trials containing code from rounds without a verified solution. Error summaries and Repair guidance are not included in this memory.
{direction}
Give only lean_strategy: a concrete Lean implementation plan for the selected goal, including definitions, intermediate Lean subgoals, lemmas, tactics, and relevant type constraints. Keep the goal unchanged. field_simp is permitted for clearing denominators; account for nonzero side conditions.
Focus on formalization. Natural Agent independently derives the mathematical proof from the problem and selected goal; do not write its proof, prescribe its reasoning, or return a separate mathematical strategy.
After selecting or receiving the task, make lean_strategy an action plan to TRY, not a truth verdict or a claim of success.
If blocked, propose a specific next candidate, subgoal, or test under the existing hypotheses. State uncertainty without replacing the plan with "the goal is false/impossible", a proof of the opposite goal, or "keep searching". Never invent a lemma or add a premise to make the attempt work.
State each needed auxiliary lemma and its applicability conditions explicitly. If its name or existence is uncertain, do not present it as an established library fact; explain how to prove the needed step from the current assumptions without adding hypotheses.
In the first round, build the Lean strategy from the supplied goal(s) and any provided problem context. From the second round onward, revise it using the same goal and previous strategies, independent natural-language arguments, and Lean code to refine the implementation plan. A previous failed code sample is not a verified result.
If previous trials exist, review their Lean strategies, independent natural-language arguments, and Lean code.
If the previous approach is mathematically sound, refine it to address the blocked steps; otherwise, choose a different approach.
Do not simply repeat the previous strategy. State concretely what you retain and what you change in this round's Lean strategy.
A failed proof does not establish the opposite claim.
Return only valid JSON: {{"task":"prove or disprove","lean_strategy":"Lean implementation strategy"}}.
Use exactly "prove" or "disprove" as task. Escape backslashes and newlines in JSON strings."""

SINGLE_DIRECTION = "Choose prove or disprove for this round; neither direction is assumed in advance."


def planner_instruction(direction, *, use_memory=True):
    """Use shared code/strategy memory, or omit history instructions in ablations."""
    instruction = PLANNER.format(direction=direction)
    if use_memory:
        return instruction
    history_lines = (
        "Read the supplied Lean goal or goals, any provided problem context, and all previous trial records.",
        "Previous records are grouped by round and direction:",
        "In the first round, build the Lean strategy",
        "If previous trials exist, review",
        "If the previous approach is mathematically sound,",
        "Do not simply repeat the previous strategy.",
    )
    instruction = "\n".join(
        line for line in instruction.splitlines()
        if not line.startswith(history_lines)
    )
    return instruction + "\nBuild this round's Lean strategy from the supplied problem context and goal(s)."

DUAL_ROLES = {
    "prove": "Your fixed task is prove. Treat the supplied prove goal as the claim to establish and actively seek its proof. Do not switch goals or stop based on an LLM truth judgment. This is a search stance, never an additional Lean premise; do not fabricate a proof.",
    "disprove": "Your fixed task is disprove. Treat the supplied disprove goal itself as the claim to establish and actively seek its proof. It already expresses the refutation of the original statement; do not negate it again. Do not switch goals or stop based on an LLM truth judgment. This is a search stance, never an additional Lean premise; do not fabricate a proof.",
}
DUAL_DIRECTION = """Your assigned_task is fixed. Read the natural-language problem as context and the assigned Lean goal in goal as your exact proof target. Treat the assigned goal as true and provable for planning purposes, and give a concrete Lean implementation strategy to establish it.
For both assigned_task=prove and assigned_task=disprove, prove goal exactly as written. The disprove goal already expresses the required refutation; do not negate or refute it again. Do not use a truth judgment about the natural-language problem to switch goals or stop the assigned attempt. Do not request the other current goal.
Use both lanes' previous records to revise your assigned approach. Do not switch goals, recommend abandonment, or replace a proof plan with a verdict that the assigned goal is false or impossible. Give concrete steps to attempt under the existing hypotheses.
Return assigned_task unchanged as task. Never add the assigned goal as a Lean hypothesis or claim an unverified attempt is a successful proof."""

DUAL_PLAN_RESTART = """Every round requires a fresh Lean strategy and an independent natural-language attempt for the same assigned goal. Previous failures or LLM truth judgments never disable this lane."""
DUAL_NATURAL_CONTINUE = """Actively attempt to prove the assigned goal itself from the problem and selected goal independently. Do not return an opposite-direction argument, a truth verdict in place of a proof attempt, or an ABANDON signal. If a step is blocked, give the concrete attempted steps and remaining mathematical obstacle without inventing facts. Your judgment cannot stop the lane: Lean generation and verification follow this stage, up to the round's attempt budget. Only a verified proof counts as success."""
DUAL_LEAN_CONTINUE = """The Natural stage has finished. Abandonment is disabled. Return a complete lean_proof for the assigned goal; do not return abandonment flags or an opposite-direction proof. Lean attempts continue until verification succeeds or the round's attempt budget is exhausted."""
DUAL_REPAIR_CONTINUE = """You cannot abandon at the Repair stage. Return retry_guidance only. Give code-repair guidance for the next Lean attempt; if the mathematical argument is obstructed, record that in the guidance without stopping the lane or changing its goal."""

NATURAL = GOAL_MEANING + """Independently read the problem and selected goal and develop a detailed, logical natural-language proof or refutation.
No Orchestrator strategy is supplied to you. Derive the mathematical argument yourself; Lean Generator separately receives the Orchestrator's Lean implementation strategy and your argument.
Produce one natural-language argument per round. It stays fixed during Lean retries in this round. Keep the selected goal unchanged.
Follow role_assumption when provided; it is a search stance, not a premise that proves the goal.
Return only the natural-language mathematical proof or refutation.
Use mathematical notation as needed, but do not output Lean code, tactic scripts, or code blocks; the Lean generator handles formalization."""

LEAN = GOAL_MEANING + """Complete the selected Lean goal using the natural-language argument, lean_strategy, and provided Lean environment.
Use lean_strategy as the implementation plan and informal as the mathematical argument to produce the final Lean proof body.
Keep the goal unchanged and formalize valid steps of the supplied argument. If the argument instead denies the assigned goal or contains an obstruction, still attempt a proof of the fixed goal using lean_strategy; never formalize the opposite claim, add premises, or fabricate a proof. field_simp is permitted for clearing denominators; discharge its side conditions.
Treat lean_strategy and repair suggestions as tentative: compiler-reported types take precedence over guessed lemma signatures. Keep consecutive top-level tactics at the same indentation.
On retries, keep the natural-language argument fixed and use the previous Lean proof, compiler diagnostics, and repair guidance to fix only the Lean implementation; preserve working parts.
On each retry, first implement the single committed correction in repair_guidance against previous_lean_proof. Treat its Before/After edits as one coordinated patch, not optional alternatives. Preserve the existing mathematical construction unless that correction identifies a concrete conflict with the fixed goal. Do not silently return an unchanged offending fragment or revert to a construction the diagnostics or guidance already ruled out. If a prescribed edit is incompatible with the actual goal or compiler-reported type, use a supported correction and add one short Lean comment at that location explaining why the prescribed edit could not be applied. Never apply a suggestion that changes the goal or invents a fact.
Apply the specific Before/After replacements in repair_guidance. Preserve tactic line breaks and indentation; do not compress consecutive tactics into one line. Return only tactic lines, without a leading by, HTML tags or Markdown. Each array item is one source line with its indentation; never substitute symbols for line breaks.
Return only valid JSON: {"lean_proof":["first tactic line","next tactic line"]}. The runtime joins these lines with real newlines after the fixed theorem header.
Escape backslashes and newlines in JSON strings. Do not add theorem headers, imports, assumptions, sorry, admit, axioms, unsafe constructs, native_decide."""

REPAIR = GOAL_MEANING + """Read the selected goal, Orchestrator lean_strategy, natural-language argument, Lean code, and verification errors.
When repair_context includes local_statement_candidates, use them as an index into the existing code, not as certified hypotheses. Check their scope and proofs. For the primary correction, inspect the immediately dependent expression and include any required side-condition discharge in the same coherent edit: fixing a type annotation alone may reveal an order, nonzero, or membership obligation. Use applicable existing conditions explicitly in that edit; derive weaker conditions from stronger ones only when valid. Do not add hypotheses, invent facts, or claim unreported code is verified.
The fixed goal and actual Lean diagnostics take priority. Both lean_strategy and the natural-language argument are tentative, not verified facts. Use the strategy to understand the intended implementation and preserve mathematically valid steps when fixing types or APIs; do not replace a valid construction solely to match the natural-language argument. If either proposal conflicts with the goal or diagnostics, explain the obstruction and suggest only supported corrections.
Your only job is to tell the Lean Generator how to retry. Do not produce an error summary or a planning-memory report. Return only valid JSON with the single string field retry_guidance, within 4000 characters. Commit to exactly one coherent correction for the next attempt, expressed as at most three minimal Before/After edits for the most important errors. Resolve API and tactic alternatives before answering; do not delegate the choice to Generator through "try X or Y", conditional fallback tactics, or a menu of constructions. If the available types do not support choosing a concrete correction, state that limitation instead of supplying competing guesses. Do not repeat edits or reproduce the full proof:

{"retry_guidance":"concrete guidance for fixing the Lean code on the next attempt"}.
In retry_guidance, identify the failing Lean step and give concrete code-level corrections while preserving the selected goal. The supplied natural-language argument stays unchanged as input, but its incorrect steps need not be followed; do not ask the Natural Agent to rewrite it at this stage. Send this guidance directly to the Lean generator; do not request a new natural-language proof or write a full replacement proof.
For each actionable error, quote the exact faulty fragment as Before and give a minimal, concrete replacement as After, with correctly oriented equalities and types. Do not include a full proof sketch in retry_guidance. For an iff theorem with explicit parameters, supply those parameters before using .mp/.mpr or .1/.2; changing only the projection spelling does not fix an unapplied function. Prefer destructuring local hypotheses or constructing witnesses directly when the goal definition permits it. Put the required edits first. Preserve valid code and line breaks; do not output HTML. If a replacement API or its type is uncertain, explain that uncertainty instead of claiming it exists. Do not change the theorem header or add hypotheses.
Before choosing an edit, inspect the current code and any supplied previous guidance, actual code diff, and repeated diagnostics. If a previously applied edit left the same error, propose a materially different correction supported by the current code and types; do not prescribe that edit again or merely re-add a setting already in scope. Do not mistake an unapplied suggestion for a failed applied edit. If the evidence does not support a new concrete correction, state that limitation briefly rather than inventing a fix.
Every Before fragment must occur in the current code and every After must actually change it. For an insertion, quote an existing anchor and include that anchor plus the inserted lines in After. Resolve competing suggestions before responding: give one consistent replacement for each selected fragment, without a later retraction or a contradictory alternative. Check these conditions while composing the retry guidance; do not add an error-summary field or claim to have executed Lean.
For each proposed function application or equality rewrite, check the current goal's type against the function's input and output types, and the left and right sides of the equality. In particular, injectivity cancels matching function applications; it does not prove an arbitrary equality between elements of the function's codomain. State the required equality orientation in the relevant edit. If elaboration cannot infer a type or instance despite an existing setting, consider making that type or instance explicit at its actual use site instead of repeating the setting.
An early elaboration failure can hide later errors. Inspect the code directly dependent on the proposed edit for clear type or equality-direction conflicts and include only supported corrections within the three-edit limit. Do not label the remaining code verified merely because it has no diagnostic. Keep unrelated definitions and proof blocks unchanged; do not rewrite the whole construction to address a local error.
For "No goals to be solved", identify redundant tactics after a goal-closing step when supported by the code and diagnostics; remove or appropriately scope them. If the argument needs mathematical revision, state the obstruction briefly within the retry guidance instead of inventing a missing assumption or lemma.
In retry_guidance, distinguish a suggested approach from a confirmed compiler fact. Do not guess extra lemma arguments; use the expected and actual types in the diagnostics.
Keep the assigned direction and goal unchanged. A failed proof does not establish the opposite claim.
Escape backslashes and newlines in JSON strings."""
