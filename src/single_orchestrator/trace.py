from __future__ import annotations

from collections.abc import Callable
import os
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

_TRACE_IO_HIDDEN_KEYS = {
    "allowed_imports",
    "expected_false_fix",
    "expected_false_transform",
    "expected_correction",
    "false_fix",
    "false_transform",
    "header",
    "langsmith_extra",
    "lean_header",
    "lean_context",
    "retrieved_imports",
    "retrieved_symbols",
    "prompt_name",
    "prompt_text",
    "prompt_version",
    "proposed_correction",
    "split",
    "strategy_round",
    "trial_index",
    "trial_label",
}


def _is_empty_optional(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {} or value == ()


def _strip_empty_optionals(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned_dict = {
            key: cleaned
            for key, item in value.items()
            if key not in _TRACE_IO_HIDDEN_KEYS
            if not _is_empty_optional(cleaned := _strip_empty_optionals(item))
        }
        if set(cleaned_dict) == {"prompt_inputs"}:
            return cleaned_dict["prompt_inputs"]
        return cleaned_dict
    if isinstance(value, list):
        return [
            cleaned
            for item in value
            if not _is_empty_optional(cleaned := _strip_empty_optionals(item))
        ]
    if isinstance(value, tuple):
        return tuple(
            cleaned
            for item in value
            if not _is_empty_optional(cleaned := _strip_empty_optionals(item))
        )
    return value


def _readable_outputs(value: Any) -> Any:
    # Public pipeline wrappers must not repeat all child-node records at the root.
    if isinstance(value, dict) and 'final_status' in value and 'trial_memory' in value:
        return {
            'result': {'solved': 'Proved', 'false': 'Disproved', 'not_solved': 'Unsolved'}.get(
                value['final_status'], value['final_status']),
            'rounds': value.get('rounds'),
            'attempts': value.get('trials'),
            'validation_conflict': value.get('validation_conflict', False),
        }
    return _strip_empty_optionals(value)


try:
    from langsmith import traceable as _langsmith_traceable
except ImportError:

    def traceable(*args: Any, **kwargs: Any) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            return func

        return decorator

else:
    def traceable(*args: Any, **kwargs: Any) -> Callable[[F], F] | F:
        if os.getenv("DUAL_ARC_LANGSMITH_FALSE_ONLY", "").lower() in {
            "1",
            "true",
            "yes",
        }:
            name = str(kwargs.get("name", args[0] if args else ""))
            if name not in {"false_reason", "false_check"}:
                def passthrough(func: F) -> F:
                    return func

                return passthrough
        kwargs.setdefault("process_inputs", _strip_empty_optionals)
        kwargs.setdefault("process_outputs", _readable_outputs)
        return _langsmith_traceable(*args, **kwargs)
