"""Bounded retries for transport/output faults, never for mathematical failure."""
import json
import time
import re
from copy import deepcopy
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone


class GoalOutputError(ValueError):
    pass


class GoalProtocolError(RuntimeError):
    """Invalid node output: reject it; never substitute a default proof or strategy."""


class GoalEnvironmentError(RuntimeError):
    """Lean execution environment failed, independently of proof correctness."""


class GoalTransportError(RuntimeError):
    """Infrastructure failure: abort evaluation rather than spend a proof attempt."""



def response_format(prompt):
    if 'strategy' in prompt.name:
        properties = {'task': {'type': 'string', 'enum': ['prove', 'disprove']},
                      'strategy': {'type': 'string', 'minLength': 1},
                      'lean_strategy': {'type': 'string', 'minLength': 1}}
        if getattr(prompt, 'version', '') in ('v19-lean-strategy-only', 'v20-bounded-repair', 'v21-guidance-only-repair'):
            properties.pop('strategy')
        return {'type': 'json_schema', 'json_schema': {'name': 'round_strategies',
                'strict': True, 'schema': {'type': 'object', 'properties': properties,
                'required': list(properties), 'additionalProperties': False}}}
    if any(n in prompt.name for n in ('lean_generator', 'retry_planner')):
        # Proof and Repair output contracts never permit abandonment.
        if getattr(prompt, 'version', '').startswith(('v18-', 'v19-', 'v20-', 'v21-')):
            fields = ('lean_proof',) if 'lean_generator' in prompt.name else (('retry_guidance',) if prompt.version == 'v21-guidance-only-repair' else ('retry_guidance', 'lean_error_summary'))
            properties = {k: {'type': 'string', 'minLength': 1} for k in fields}
            if 'lean_generator' in prompt.name and prompt.version in ('v18-format-repair-lines', 'v18-forced-goal-lines', 'v19-lean-strategy-only', 'v20-bounded-repair', 'v21-guidance-only-repair'):
                properties['lean_proof'] = {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1}
            if getattr(prompt, 'version', '') in ('v20-bounded-repair', 'v21-guidance-only-repair') and 'retry_planner' in prompt.name:
                properties['retry_guidance']['maxLength'] = 4000
                if 'lean_error_summary' in properties:
                    properties['lean_error_summary']['maxLength'] = 800
            return {'type': 'json_schema', 'json_schema': {
                'name': 'lean_proof_body' if 'lean_generator' in prompt.name else 'lean_repair_guidance',
                'strict': True, 'schema': {'type': 'object',
                    'properties': properties,
                    'required': list(fields), 'additionalProperties': False}}}
        return {'type': 'json_object'}
    return None


def validate_natural_output(prompt, raw):
    if 'nature_agent' not in prompt.name:
        return
    if re.search(r"```|~~~|:=\s*by\b|(?m:^\s*(?:theorem|lemma|example)\s+)", raw):
        raise GoalOutputError('Natural output must be prose, not Lean code or code blocks')
    # In forced-goal runs, a stop-like prose output is retained for audit,
    # but never controls execution. Lean must still attempt the fixed goal.
    if getattr(prompt, 'version', '') in ('v18-forced-goal-lines', 'v19-lean-strategy-only', 'v20-bounded-repair', 'v21-guidance-only-repair'):
        return
    if raw.strip().startswith('ABANDON:'):
        from .goal_search import GoalSearch
        if (not prompt.name.startswith(('truth_', 'falsity_')) or
                not GoalSearch.abandonment_reason(raw, prompt.inputs.get('task'))):
            raise GoalOutputError('Invalid Natural abandonment; attempt the assigned goal')


def validate_response(prompt, response):
    if not response.choices:
        raise GoalOutputError('Provider returned no choices')
    choice = response.choices[0]
    if choice.finish_reason in ('error', 'length'):
        raise GoalOutputError('Provider finish_reason=' + choice.finish_reason)
    raw = choice.message.content or ''
    if not isinstance(raw, str) or not raw.strip():
        raise GoalOutputError('Provider returned no final text')
    validate_natural_output(prompt, raw)
    if response_format(prompt):
        def unique(pairs):
            obj = {}
            for k, v in pairs:
                if k in obj:
                    raise GoalOutputError('Duplicate JSON field: ' + k)
                obj[k] = v
            return obj
        parsed = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(parsed, dict):
            raise GoalOutputError('Expected a JSON object')
        if parsed.get('abandon') is True:
            if getattr(prompt, 'version', '').startswith(('v18-', 'v19-', 'v20-', 'v21-')):
                raise GoalOutputError('Abandonment is disabled' if prompt.version in ('v18-forced-goal-lines', 'v19-lean-strategy-only', 'v20-bounded-repair', 'v21-guidance-only-repair') else 'Abandonment is allowed only at the Natural stage')
            if 'strategy' in prompt.name or not prompt.name.startswith(('truth_', 'falsity_')):
                raise GoalOutputError('Abandonment is only allowed in Dual lane work')
            required = ('reason', 'retry_guidance', 'lean_error_summary') if 'retry_planner' in prompt.name else ('reason',)
        else:
            required = tuple(response_format(prompt)['json_schema']['schema']['required']) if 'strategy' in prompt.name else ('lean_proof',) if 'lean_generator' in prompt.name else (('retry_guidance',) if prompt.version == 'v21-guidance-only-repair' else ('retry_guidance', 'lean_error_summary'))
        for key in required:
            if key == 'lean_proof' and getattr(prompt, 'version', '') in ('v18-format-repair-lines', 'v18-forced-goal-lines', 'v19-lean-strategy-only', 'v20-bounded-repair', 'v21-guidance-only-repair'):
                lines = parsed.get(key)
                if not isinstance(lines, list) or not lines or not all(isinstance(line, str) for line in lines) or not any(line.strip() for line in lines):
                    raise GoalOutputError('lean_proof must be a nonempty array of tactic lines')
                continue
            # An abandoning Repair may have empty retry guidance.
            if key == 'retry_guidance' and parsed.get('abandon') is True:
                if isinstance(parsed.get(key), str):
                    continue
            if not isinstance(parsed.get(key), str) or not parsed[key].strip():
                raise GoalOutputError('Missing or empty JSON field: ' + key)
            maximum = response_format(prompt).get('json_schema', {}).get('schema', {}).get('properties', {}).get(key, {}).get('maxLength')
            if maximum is not None and len(parsed[key]) > maximum:
                raise GoalOutputError(f'JSON field {key} exceeds {maximum} characters')
        if getattr(prompt, 'version', '') in ('v18-format-repair-lines', 'v18-forced-goal-lines', 'v19-lean-strategy-only', 'v20-bounded-repair', 'v21-guidance-only-repair'):
            if set(parsed) != set(required):
                raise GoalOutputError('Unexpected node output fields; goal/task overrides are forbidden')
            if 'lean_generator' in prompt.name:
                from .lean_format import normalize_lean_proof
                from .formal_false_review import _without_lean_comments
                body, _ = normalize_lean_proof('\n'.join(parsed['lean_proof']))
                if not body:
                    raise GoalOutputError('Empty tactic body after decoding')
                if body.startswith(('```', '~~~')) or body.endswith(('```', '~~~')):
                    raise GoalOutputError('Lean output must contain source lines, not Markdown fences')
                prohibited = re.search(r"\b(sorry|admit|axiom|opaque|unsafe|constant|native_decide|run_tac|run_elab|run_meta|elab|macro)\b|(?m:^\s*(?:import|open|theorem|lemma|namespace|section|end|set_option|def|abbrev|instance|#)\b)", _without_lean_comments(body))
                if prohibited:
                    raise GoalOutputError('Forbidden proof packaging or construct: ' + prohibited.group(0))
        if 'strategy' in prompt.name:
            task = parsed['task']
            if task not in ('prove', 'disprove') or task != prompt.inputs.get('assigned_task', task):
                raise GoalOutputError('Planner changed the assigned direction')
    return raw


def complete_with_retries(prompt, request, *, max_attempts=3,
                          max_transport_attempts=5, on_attempt=None,
                          on_output_error=None, wait=time.sleep):
    """Keep output and transport recovery budgets separate; log every request.

    Transport failures never consume one of the model-output opportunities.
    Exhausted infrastructure recovery propagates outside proof-search fallbacks.
    """
    if not 1 <= max_attempts <= 3:
        raise ValueError('Output attempts must be between 1 and 3')
    if not 1 <= max_transport_attempts <= 10:
        raise ValueError('Transport attempts must be between 1 and 10')
    attempt = output_errors = transport_errors = 0
    while True:
        attempt += 1
        response = None
        try:
            response = request()
            raw = validate_response(prompt, response)
        except Exception as exc:
            from .local_audit import emit
            emit('api_attempt_rejected', attempt=attempt, prompt_name=prompt.name,
                 error_type=type(exc).__name__, error=str(exc),
                 status_code=getattr(exc, 'status_code', None))
            if on_attempt:
                on_attempt(attempt, response, exc)
            status = getattr(exc, 'status_code', None)
            transport = (status is not None or isinstance(exc, (ConnectionError, TimeoutError)) or
                         'connection' in type(exc).__name__.lower() or
                         'timeout' in type(exc).__name__.lower())
            if transport:
                transport_errors += 1
                retryable = status is None or status in (408, 409, 429) or status >= 500
                if not retryable or transport_errors >= max_transport_attempts:
                    raise GoalTransportError(
                        f'Infrastructure request failed after {transport_errors} transport failure(s); '
                        'no proof outcome may be scored from this run. '
                        f'{type(exc).__name__}: {exc}') from exc
                delay = min(60, 5 * 2 ** (transport_errors - 1))
                headers = getattr(getattr(exc, 'response', None), 'headers', {}) or {}
                retry_after = headers.get('retry-after', headers.get('Retry-After'))
                if retry_after is not None:
                    try:
                        delay = max(delay, float(retry_after))
                    except (TypeError, ValueError):
                        try:
                            until = parsedate_to_datetime(retry_after)
                            if until.tzinfo is None:
                                until = until.replace(tzinfo=timezone.utc)
                            delay = max(delay, (until - datetime.now(timezone.utc)).total_seconds())
                        except (TypeError, ValueError, OverflowError):
                            pass
                emit('api_recovery_wait', prompt_name=prompt.name, kind='transport',
                     delay_seconds=delay, retry_after=retry_after,
                     transport_errors=transport_errors, output_errors=output_errors)
                # Respect long Retry-After values without a single >60s sleep.
                while delay > 0:
                    chunk = min(60, delay)
                    wait(chunk)
                    delay -= chunk
            else:
                output_errors += 1
                retryable = isinstance(exc, (GoalOutputError, json.JSONDecodeError))
                if (not retryable or output_errors >= max_attempts or
                        ('finish_reason=length' in str(exc) and getattr(prompt, 'version', '') not in ('v20-bounded-repair', 'v21-guidance-only-repair'))):
                    if (getattr(prompt, 'version', '') in ('v18-format-repair-lines', 'v18-forced-goal-lines', 'v19-lean-strategy-only', 'v20-bounded-repair', 'v21-guidance-only-repair') and
                            isinstance(exc, (GoalOutputError, json.JSONDecodeError))):
                        raise GoalProtocolError(f'Node output contract failed for {prompt.name}; reject this output: {exc}') from exc
                    raise
                if on_output_error:
                    on_output_error(response, exc)
                emit('api_recovery_wait', prompt_name=prompt.name, kind='output',
                     delay_seconds=0.5*output_errors,
                     transport_errors=transport_errors, output_errors=output_errors)
                wait(0.5 * output_errors)
        else:
            if on_attempt:
                on_attempt(attempt, response, None)
            return raw, response


def output_correction(prompt, error):
    """Concrete output repair, never mathematical or compiler feedback."""
    if 'lean_generator' in prompt.name:
        shape = ('{"lean_proof":["tactic line", "next tactic line"]}'
                 if getattr(prompt, 'version', '') in ('v18-format-repair-lines', 'v18-forced-goal-lines', 'v19-lean-strategy-only', 'v20-bounded-repair', 'v21-guidance-only-repair')
                 else '{"lean_proof":"tactic body"}')
        contract = (f'Return only JSON {shape}. '
                    'Supply only the proof body after the fixed theorem header. '
                    'Remove theorem/lemma declarations, imports, and a leading by; '
                    'do not change the supplied goal or add assumptions. '
                    'Do not use sorry, admit, axioms, or other prohibited constructs.')
    elif 'strategy' in prompt.name:
        fields = 'task and lean_strategy' if getattr(prompt, 'version', '') in ('v19-lean-strategy-only', 'v20-bounded-repair', 'v21-guidance-only-repair') else 'task, strategy, and lean_strategy'
        contract = (f'Return only JSON with {fields}. '
                    'Use nonempty strings and keep assigned_task unchanged when provided.')
    elif 'retry_planner' in prompt.name:
        contract = ('Return only JSON with the single nonempty string field retry_guidance. Do not summarize errors, abandon, or change the goal.'
                    if prompt.version == 'v21-guidance-only-repair' else
                    'Return only JSON with the two nonempty string fields retry_guidance and lean_error_summary. Do not abandon or change the goal.')
        if getattr(prompt, 'version', '') in ('v20-bounded-repair', 'v21-guidance-only-repair'):
            contract += ' Keep retry_guidance within 4000 characters. Give at most three minimal Before/After edits.'
            if prompt.version != 'v21-guidance-only-repair':
                contract += ' Keep lean_error_summary within 800 characters.'
    else:
        contract = 'Return only a natural-language argument, without Lean code or code blocks.'
        if getattr(prompt, 'version', '') in ('v18-forced-goal-lines', 'v19-lean-strategy-only', 'v20-bounded-repair', 'v21-guidance-only-repair'):
            contract += ' Attempt the assigned goal itself. Abandonment is disabled; do not negate the goal again.'
        elif prompt.name.startswith(('truth_', 'falsity_')):
            verdict = 'false' if prompt.inputs.get('task') == 'prove' else 'true'
            contract += (f' If mathematically justified abandonment is needed, begin exactly '
                         f'with "ABANDON: The original statement is {verdict}." '
                         'and then give the specific mathematical reason.')
    if 'finish_reason=length' in str(error):
        contract += ' The previous response was truncated. Regenerate shorter complete JSON within the output-token budget. Do not repeat text or continue the incomplete response.'
    return ('Your previous output was rejected before Lean execution. '
            f'Output validation error: {error}\n{contract}\n'
            'Correct this output-contract violation using the original task. '
            'The rejected output is not evidence about the truth of the goal.')


def complete_chat_with_retries(prompt, create, *, request_options=None,
                               on_attempt=None, on_request=None,
                               max_attempts=3, max_transport_attempts=5,
                               wait=time.sleep):
    """Chat adapter with rejection feedback; preserve all original task inputs.

    on_request receives the exact messages for per-request logging. Only the
    latest invalid output is retained, so recovery does not grow trial memory.
    Transport retries reuse the same messages and do not invent output feedback.
    """
    original = [{"role": "user", "content": prompt.text}]
    messages = deepcopy(original)
    options = dict(request_options or {})
    if 'messages' in options:
        raise ValueError('messages are managed by the output-recovery adapter')
    request_number = 0

    def request():
        nonlocal request_number
        request_number += 1
        payload = deepcopy(messages)
        if on_request:
            on_request(request_number, deepcopy(payload))
        return create(messages=payload, **options)

    def rejected(response, error):
        raw = response.choices[0].message.content if response and response.choices else None
        messages[:] = deepcopy(original)
        # Truncated/huge responses stay in the audit log, never in the next prompt.
        if isinstance(raw, str) and raw.strip() and len(raw) <= 12000 and 'finish_reason=length' not in str(error):
            messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": output_correction(prompt, error)})

    return complete_with_retries(prompt, request, max_attempts=max_attempts,
        max_transport_attempts=max_transport_attempts, on_attempt=on_attempt,
        on_output_error=rejected, wait=wait)
