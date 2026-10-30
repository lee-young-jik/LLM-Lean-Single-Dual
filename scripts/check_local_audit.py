"""Offline regression checks for durable, parallel, failed and recovered runs."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'scripts'))
os.environ['LANGSMITH_TRACING']='false'
from single_orchestrator.goal_search import GoalSearch
from single_orchestrator.memory import JsonlTrialMemory
from single_orchestrator.model import OpenAICompatibleModelClient
from single_orchestrator.local_audit import AuditSession,SESSION
from single_orchestrator.prompts import BuiltPrompt
from analyze_logged_experiment import collect,analyze,mcnemar,metrics

class Client:
    def complete(self,p):
        if 'strategy' in p.name:
            return json.dumps(dict(task=p.inputs.get('assigned_task','prove'),lean_strategy='Use trivial.'))
        if 'nature_agent' in p.name:return 'Prove the assigned target directly.'
        if 'lean_generator' in p.name:return json.dumps({'lean_proof':['trivial']})
        return json.dumps({'retry_guidance':'Try the target again.'})
class Verifier:
    prelude=''
    def verify(self,code,formal_statement=''):
        return dict(success=False,returncode=1,formal_state=code,timed_out=False,lean_error_log='ordinary Lean type error')

with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);os.environ['DUAL_ARC_EXPERIMENT_ID']='offline_test'
    for mode in ('single','dual'):
        for condition in ('full','none'):
            os.environ['DUAL_ARC_MEMORY_CONDITION']=condition
            folder=root/mode/condition;folder.mkdir(parents=True)
            engine=GoalSearch(model_client=Client(),verifier=Verifier(),mode=mode,max_rounds=2,max_trials=3,
                memories={k:JsonlTrialMemory(folder/f'fixture.{k}_memory.jsonl') for k in ('prove','disprove')})
            result=engine.run(problem='Public',formal_statement='theorem fixture : True := by')
            audit=Path(result['local_audit_dir']);events=[json.loads(s) for s in (audit/'events.jsonl').read_text().splitlines()]
            starts=[e for e in events if e['event']=='node_started'];ends=[e for e in events if e['event']=='node_finished']
            assert {e['node_id'] for e in starts}=={e['node_id'] for e in ends}
            assert len({e['sequence'] for e in events})==len(events)
            plans=[e for e in starts if 'strategy' in e.get('metadata',{}).get('node','') and e['metadata']['round']==2]
            assert all(bool(e['inputs'].get('trial_memory'))==(condition=='full') for e in plans)
            assert sum(e['event']=='lean_verification' for e in events)==(6 if mode=='single' else 12)
            assert sum(e['event']=='model_raw_output' for e in events)==len([e for e in starts if e['run_type']=='llm'])
            assert (audit/'trace.md').exists() and (audit/'source_snapshot/goal_search.py').exists()
    # Record failure before the first completed trial, never fabricate Unsolved.
    class Broken(Client):
        def complete(self,p):raise RuntimeError('Injected infrastructure fault')
    engine.model_client=Broken()
    try:engine.run(problem='Public',formal_statement='theorem fixture : True := by')
    except RuntimeError:pass
    else:raise AssertionError('Fatal error swallowed')
    outcomes=[json.loads(p.read_text()) for p in root.rglob('outcome.json')]
    assert sum(o['execution_status']=='execution_error' for o in outcomes)==1
    # Real OpenAI response type; invalid response and recovery are both billed/logged.
    from openai.types.chat import ChatCompletion
    session=AuditSession(root/'api',{});token=SESSION.set(session)
    def response(content):
        return ChatCompletion.model_validate({'id':'fake-id','object':'chat.completion','created':0,'model':'fake',
            'provider':'fake-provider','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':content}}],
            'usage':{'prompt_tokens':5,'completion_tokens':3,'total_tokens':8,'cost':.01}})
    calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        return response('{"lean_proof":[]}' if len(calls)==1 else '{"lean_proof":["trivial"]}')
    fake=NS(base_url='https://openrouter.ai/api/v1',chat=NS(completions=NS(create=create)))
    os.environ['AUDIT_TEST_API_KEY']='secret-test-credential'
    try:
        with patch('openai.OpenAI',return_value=fake),patch('single_orchestrator.goal_api.time.sleep'):
            client=OpenAICompatibleModelClient(model='fake',api_key_env='AUDIT_TEST_API_KEY')
            prompt=BuiltPrompt(name='lean_generator.generate',version='v18-forced-goal-lines',inputs={},text='Public prompt')
            assert 'trivial' in client.complete(prompt)
        session.finish('completed')
    finally:SESSION.reset(token)
    events=[json.loads(s) for s in (session.path/'events.jsonl').read_text().splitlines()]
    assert sum(e['event']=='api_request' for e in events)==2
    assert sum(e['event']=='api_response' for e in events)==2
    assert sum(e['event']=='api_attempt_rejected' for e in events)==1
    assert 'Correct this output-contract violation' in calls[1]['messages'][-1]['content']
    assert 'secret-test-credential' not in (session.path/'events.jsonl').read_text()
    assert next(e for e in events if e['event']=='api_response')['response']['usage']['cost']==.01
    os.environ.pop('AUDIT_TEST_API_KEY')
    # Statistical edge cases and abstention handling.
    assert mcnemar(0,0)==1 and abs(mcnemar(0,6)-.03125)<1e-12
    rows=[dict(valid=True,truth='disprove',prediction='unsolved',correct=False,rounds=3,
               repetition='1',problem_id='a',wall_seconds=1,lean_builds=1,api_requests=1,api_errors=0,
               output_rejections=0,prompt_tokens=None,completion_tokens=None,reasoning_tokens=None,cost_usd=None)]
    m=metrics(rows)
    assert m['classes']['disprove']['fn_including_unsolved']==1
    assert m['classes']['disprove']['precision'] is None
    assert m['cost_usd']['sum_reported'] is None
    assert m['pipeline_pass_at_k']['5']['value'] is None
os.environ.pop('DUAL_ARC_MEMORY_CONDITION',None)
print('PASS: durable node/raw/API/Lean logs; dual concurrency; full/no-memory; fatal errors; rejected output recovery + cost; secrets excluded; statistical/abstention edge cases. No network requests.')
