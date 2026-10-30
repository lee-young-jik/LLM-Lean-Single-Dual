"""Readable node traces with explicit inputs and outputs, never prompt duplication."""
from contextlib import contextmanager
import json


def node_name(name):
    if 'strategy' in name:
        return '01 · Orchestrator | Lean 전략 수립'
    if 'nature_agent' in name:
        return '02 · Natural Agent | 자연어 증명'
    if 'lean_generator' in name:
        return '03 · Lean Generator | Lean 코드 생성'
    if 'verifier' in name:
        return '04 · Lean Verifier | Lean 검증'
    if 'retry_planner' in name:
        return '05 · Repair Agent | 수정 지침'
    return name


def node_output(name, raw, *, version=None):
    if 'retry_planner' in name:
        from .false_review import FalseReviewRuntime
        try:
            parsed = FalseReviewRuntime._parse_json(raw)
            keys = ('retry_guidance',) if version == 'v21-guidance-only-repair' else ('retry_guidance','lean_error_summary','abandon','reason')
            return {k:parsed[k] for k in keys if k in parsed}
        except (ValueError,TypeError,json.JSONDecodeError):
            # Preserve readable historical traces that used plain-text repair.
            return {'revision_guidance':raw}
    if 'strategy' in name or 'lean_generator' in name:
        from .false_review import FalseReviewRuntime
        try:
            parsed = FalseReviewRuntime._parse_json(raw)
            keys = ('task', 'strategy', 'lean_strategy', 'abandon', 'reason') if 'strategy' in name else ('lean_proof', 'abandon', 'reason')
            return {k: parsed[k] for k in keys if k in parsed}
        except (ValueError, TypeError, json.JSONDecodeError):
            return {'unparsed_output': raw}
    if 'nature_agent' in name and version in ('v18-forced-goal-lines', 'v19-lean-strategy-only', 'v20-bounded-repair', 'v21-guidance-only-repair'):
        return {'informal_proof': raw}
    if name.startswith(('truth_', 'falsity_')) and raw.strip().startswith('ABANDON:'):
        return {'abandoned':True,'reason':raw.strip()[len('ABANDON:'):].strip()}
    return {'informal_proof' if 'nature_agent' in name else 'revision_guidance': raw}


@contextmanager
def node_trace(name, inputs, *, metadata=None, run_type='chain'):
    """Always save locally; remote tracing is an additional presentation layer."""
    from .local_audit import NODE, emit
    from uuid import uuid4
    from time import monotonic
    node_id = uuid4().hex
    parent = NODE.get()
    token = NODE.set(node_id)
    started = monotonic()
    emit('node_started', name=name, parent_node_id=parent, inputs=inputs,
         metadata=metadata or {}, run_type=run_type)
    try:
        try:
            from langsmith import trace
        except ImportError:
            yield None
        else:
            with trace(name, run_type=run_type, inputs=inputs, metadata=metadata or {}) as run:
                yield run
    except BaseException as exc:
        emit('node_error', name=name, error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        emit('node_finished', name=name, duration_seconds=monotonic()-started)
        NODE.reset(token)


def finish(run, outputs):
    from .local_audit import emit
    emit('node_output', outputs=outputs)
    if run is not None:
        run.end(outputs=outputs)
