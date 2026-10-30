"""Check source mapping, real diagnostics and symmetric integration without API calls."""
from pathlib import Path
import json
import os
import sys
import tempfile
import importlib.util
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
os.environ['LANGSMITH_TRACING'] = 'false'
from single_orchestrator.repair_context import build_repair_context, diagnostics
from single_orchestrator.goal_search import GoalSearch
from single_orchestrator.memory import JsonlTrialMemory

source = 'import Mathlib\ntheorem test : True := by\n  exact missing\n#print axioms test'
log = 'error: Scratch.lean:3:8: Unknown identifier `missing`\nerror: Lean exited with code 1\nerror: build failed'
prior = {'lean_proof': 'exact old', 'lean_error_log': log, 'retry_plan': 'Before: old; After: missing'}
ctx = build_repair_context(lean_proof='exact missing', formal_state=source,
    error_log=log, previous=prior, source_file='/project/Scratch.lean')
assert ctx['diagnostics_total'] == 1
assert ctx['diagnostics'][0]['compiled_source_excerpt'][2] == {'line': 3, 'text': '  exact missing'}
assert ctx['diagnostics'][0]['repeated_from_previous_attempt']
assert '-exact old' in ctx['previous_to_current_tactic_diff']
assert '+exact missing' in ctx['previous_to_current_tactic_diff']
assert diagnostics('Scratch.lean:3:8: error(lean.unknownIdentifier): Unknown identifier `missing`')[0]['line'] == 3
assert diagnostics('info: Scratch.lean:4:0: depends on axioms: [sorryAx]') == []
assert diagnostics('warning: Scratch.lean:3:0: unused variable') == []
assert diagnostics('Scratch.lean:3:0: warning: unused variable') == []
other = build_repair_context(lean_proof='', formal_state=source,
    error_log=log, source_file='Other.lean')
assert 'compiled_source_excerpt' not in other['diagnostics'][0]
assert not build_repair_context(lean_proof='', formal_state='', error_log=log,
    source_file='Scratch.lean')['diagnostics'][0].get('compiled_source_excerpt')
many = build_repair_context(lean_proof='x'*20000, formal_state=source,
    error_log='\n'.join(f'error: Scratch.lean:3:8: failure {n}' for n in range(30)),
    previous=prior, source_file='Scratch.lean')
assert len(many['diagnostics']) == 6 and many['diagnostics_omitted'] == 24
assert many['diff_truncated'] and len(many['previous_to_current_tactic_diff']) == 6000
# Historical real Lean output maps to the exact compiled source, including prelude/header.
fixture = ROOT / '.runs/shared_backend_support_20261016/linear_algebra_5A_4_dual/result.json'
if fixture.exists():
    record = json.loads(fixture.read_text())['trial_memory'][0]
    errors = diagnostics(record['lean_error_log'])
    assert len(errors) == 1, errors
    actual = build_repair_context(lean_proof=record['lean_proof'], formal_state=record['formal_state'],
        error_log=record['lean_error_log'], source_file=errors[0]['source_file'])
    error = actual['diagnostics'][0]
    assert any(x['line'] == error['line'] and 'hre' in x['text'] for x in error['compiled_source_excerpt'])

class Client:
    def __init__(self): self.calls = []
    def complete(self, p):
        self.calls.append(p)
        if 'strategy' in p.name:
            return json.dumps({'task': p.inputs.get('assigned_task', 'prove'), 'lean_strategy': 'Try the goal.'})
        if 'nature_agent' in p.name: return 'Attempt an elementary proof of the assigned statement.'
        if 'lean_generator' in p.name: return json.dumps({'lean_proof': ['exact missing']})
        return json.dumps({'retry_guidance': 'Before: missing; After: other'})

class Verifier:
    prelude = ''
    lean_file = Path('Scratch.lean')
    def verify(self, source, formal_statement):
        return {'success': False, 'returncode': 1, 'formal_state': source,
                'lean_error_log': log, 'timed_out': False}

contexts = {}
repair_instructions = {}
with tempfile.TemporaryDirectory() as tmp:
    for enabled in ('0', '1'):
        for mode in ('single', 'dual'):
            folder = Path(tmp) / (mode + enabled)
            folder.mkdir()
            client = Client()
            with patch.dict(os.environ, {'DUAL_ARC_REPAIR_CONTEXT': enabled, 'DUAL_ARC_LEAN_DECLARATIONS': '0'}):
                engine = GoalSearch(model_client=client, verifier=Verifier(), mode=mode, max_trials=2, max_rounds=1,
                    memories={t: JsonlTrialMemory(folder / (t+'.jsonl')) for t in ('prove', 'disprove')})
                result = engine.run(problem='Public', formal_statement='theorem fixture : (2 : Nat) = 3 := by')
            assert result['trials'] == (2 if mode == 'single' else 4)
            for p in client.calls:
                if 'nature_agent' in p.name or 'strategy' in p.name:
                    assert 'repair_context' not in p.inputs
                if 'nature_agent' in p.name:
                    assert 'lean_strategy' not in p.inputs
                if 'retry_planner' in p.name:
                    assert p.inputs['lean_strategy'] == 'Try the goal.'
                    generated = [g for g in client.calls if 'lean_generator' in g.name and g.inputs['goal'] == p.inputs['goal']]
                    assert generated and all(g.inputs['lean_strategy'] == p.inputs['lean_strategy'] for g in generated)
                    from single_orchestrator import goal_prompts
                    common = p.text.split('\nDo not use tools, hidden labels or stored answers.')[0]
                    if mode == 'dual':
                        common = common.removesuffix('\n' + goal_prompts.DUAL_REPAIR_CONTINUE)
                    repair_instructions[mode] = common
                    assert 'Both lean_strategy and the natural-language argument are tentative' in p.text
                    assert ('repair_context' in p.inputs) == (enabled == '1')
                if 'lean_generator' in p.name and 'previous_lean_proof' in p.inputs:
                    assert ('repair_context' in p.inputs) == (enabled == '1')
            if enabled == '1':
                contexts[mode] = [p.inputs['repair_context'] for p in client.calls
                    if 'retry_planner' in p.name and p.inputs['task'] == 'prove']
    assert contexts['single'] == contexts['dual']
    assert repair_instructions['single'] == repair_instructions['dual']
print('PASS: bounded source locations, typed diagnostics, file mismatch, real-log mapping, edit diff, repetition, on/off switch, equal lane inputs and unchanged budgets; zero model calls.')

# Exercise the general launcher up to its real subprocess boundary, without calls.
spec = importlib.util.spec_from_file_location('logged_experiment', ROOT/'scripts/run_logged_experiment.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
with tempfile.TemporaryDirectory() as tmp:
    folder = Path(tmp)
    public = folder/'public.jsonl'
    prelude = 'import Mathlib\nopen Filter\nopen scoped Topology\n'
    public.write_text(json.dumps({'name': 'fixture', 'split': 'test', 'header': prelude,
        'formal_statement': 'theorem fixture : True := by', 'informal_prefix': 'True.'})+'\n')
    settings = {'NATURAL_MODEL': 'worker', 'LEAN_MODEL': 'worker',
        'RETRY_MODEL': 'worker', 'ORCHESTRATOR_MODEL': 'planner',
        'DUAL_ARC_LEAN_DECLARATIONS': '1', 'DUAL_ARC_REPAIR_CONTEXT': '1',
        'DUAL_ARC_REPAIR_MAX_OUTPUT_TOKENS': '2048',
        'DUAL_ARC_REPAIR_TIMEOUT_SECONDS': '180', 'DUAL_ARC_REASONING_EFFORT': ''}
    for enabled in ('0', '1'):
        settings['DUAL_ARC_REPAIR_CONTEXT'] = enabled
        argv = ['run_logged_experiment', '--jsonl', str(public), '--split', 'test',
            '--ids', 'fixture', '--project-root', str(folder), '--output-dir',
            str(folder/('out'+enabled)), '--repetitions', '1', '--run']
        with patch.object(sys, 'argv', argv), patch.object(launcher, 'dotenv_values', return_value=settings), \
                patch.object(launcher, 'load_dotenv'), patch.object(launcher.subprocess, 'run',
                return_value=SimpleNamespace(returncode=0)) as run:
            launcher.main()
        assert run.call_count == 2
        for call in run.call_args_list:
            command = call.args[0]
            assert command[command.index('--verifier-prelude') + 1] == prelude
            env = call.kwargs['env']
            for key, value in settings.items():
                if key.startswith('DUAL_ARC_'): assert env[key] == value, key
print('PASS: general experiment launcher forwards support off/on and bounded Repair settings to both CLI subprocesses; zero model calls.')
