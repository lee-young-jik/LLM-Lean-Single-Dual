"""Compact, explicit model inputs; full audit records remain on disk."""
from copy import deepcopy


def planner_memory(history, goals=None):
    """Share each round/direction's plans and argument across Lean attempts."""
    groups = {}
    for record in history:
        round_index = record.get('strategy_round', record.get('round'))
        task = record.get('task')
        strategy = record.get('strategy')
        lean_strategy = record.get('lean_strategy')
        # Accept raw trials, current groups, and historical groups whose
        # natural-language argument still changed on every retry.
        for attempt in record.get('trials', [record]):
            informal = attempt.get('informal', record.get('informal'))
            # Preserve distinct contexts in imported historical records.
            key = (round_index, task, strategy, lean_strategy, informal)
            if key not in groups:
                group = {'round': round_index, 'task': task,
                         'strategy': strategy, 'lean_strategy': lean_strategy,
                         'informal': informal}
                groups[key] = {k: v for k, v in group.items()
                               if v is not None and v != ''}
                groups[key]['trials'] = []
            verification = attempt.get('verifier_result') or {}
            trial = {
                'trial': attempt.get('trial_index', attempt.get('trial')),
                'lean_code': attempt.get('lean_code') or attempt.get('formal_state') or verification.get('formal_state'),
            }
            groups[key]['trials'].append(deepcopy({k: v for k, v in trial.items() if v is not None and v != ''}))
    return deepcopy(list(groups.values()))
