"""Regression tests for legitimate Lean tactics and bounded truncation recovery."""
from pathlib import Path
import json, os, sys, tempfile
from types import SimpleNamespace as NS
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
os.environ['LANGSMITH_TRACING']='false'
from single_orchestrator.goal_api import complete_chat_with_retries, GoalProtocolError, response_format
from single_orchestrator.prompts import BuiltPrompt
from single_orchestrator.model import OpenAICompatibleModelClient, _output_token_limit
from single_orchestrator.proof_audit import audit_lean_proof
from single_orchestrator.goal_search import GoalVerifier
from single_orchestrator.formal_false_review import FormalFalseReviewRuntime
p=BuiltPrompt(name='truth_retry_planner.plan',version='v20-bounded-repair',inputs={'task':'prove','goal':'theorem target : True := by'},text='Fixed goal and original compiler errors')
def response(raw,finish='stop'):return NS(choices=[NS(finish_reason=finish,message=NS(content=raw))])
valid=json.dumps({'retry_guidance':'Before: exact missing\nAfter: trivial','lean_error_summary':'unknown identifier missing'})
requests=[];seq=iter([response('runaway '*40000,'length'),response(valid)])
def create(**kw):requests.append(kw);return next(seq)
raw,_=complete_chat_with_retries(p,create,request_options={'max_tokens':2048},wait=lambda _:None)
assert raw==valid and len(requests)==2
assert all(r['max_tokens']==2048 for r in requests)
assert len(requests[1]['messages'])==2 and requests[1]['messages'][0]['content']==p.text
assert 'truncated' in requests[1]['messages'][-1]['content'] and 'shorter complete JSON' in requests[1]['messages'][-1]['content']
assert 'runaway' not in json.dumps(requests[1])
requests=[]
def truncated(**kw):requests.append(kw);return response('x'*20000,'length')
try:complete_chat_with_retries(p,truncated,max_attempts=3,wait=lambda _:None)
except GoalProtocolError:pass
else:raise AssertionError('Truncated output accepted')
assert len(requests)==3, 'Output recovery must remain bounded'
seq=iter([response(json.dumps({'retry_guidance':'x'*4001,'lean_error_summary':'error'})),response(valid)])
raw,_=complete_chat_with_retries(p,lambda **kw:next(seq),wait=lambda _:None)
assert raw==valid
assert response_format(p)['json_schema']['schema']['properties']['retry_guidance']['maxLength']==4000
# Verify the actual SDK options and timeout, independent of a launch script.
calls=[];constructors=[]
def sdk_create(**kw):calls.append(kw);return response(valid)
def sdk(**kw):constructors.append(kw);return NS(base_url='https://openrouter.ai/api/v1',chat=NS(completions=NS(create=sdk_create)))
with patch.dict(os.environ,{'BOUNDED_REPAIR_TEST_KEY':'test-only','DUAL_ARC_MAX_OUTPUT_TOKENS':'16384','DUAL_ARC_REPAIR_MAX_OUTPUT_TOKENS':'2048','DUAL_ARC_REPAIR_TIMEOUT_SECONDS':'180'}),patch('openai.OpenAI',side_effect=sdk):
    c=OpenAICompatibleModelClient(model='openai/gpt-6-luna',timeout_seconds=0,api_key_env='BOUNDED_REPAIR_TEST_KEY')
    assert c.complete(p)==valid
assert calls[0]['max_tokens']==2048 and constructors[0]['timeout']==180
assert _output_token_limit(BuiltPrompt(name='lean_generator.generate',version=p.version,inputs={},text='target'))==16384
# Sound Lean syntax reaches Lean; genuine escape hatches and target changes do not.
target='theorem local_assumption (P : Prop) (h : P) : P := by'
assert audit_lean_proof(target+'\n  exact (by assumption)',target).success
assert not audit_lean_proof(target+'\n  sorry',target).success
assert not audit_lean_proof('theorem changed : True := by trivial',target).success
assert not audit_lean_proof('axiom extra : False\n'+target+'\n  exact extra.elim',target).success
print('PASS: truncated output regenerated without replaying it; bounded retry exhaustion; field limits; actual SDK Repair cap/timeout; fixed target and placeholder guards. Zero model calls.',flush=True)
# These checks use real Lean, including a sound use and an unsuccessful use.
project=ROOT/'.datasets/miniF2F-lean4';module='MiniF2F.Scratch.BoundedRepairRegression'
v=GoalVerifier(project_root=project,module_name=module,lean_file=project/(module.replace('.','/')+'.lean'),prelude='import Mathlib.Logic.Basic',timeout_seconds=120)
kernel=FormalFalseReviewRuntime(None,v)
checks=[(target,'exact (by assumption)',True),
        ('theorem local_false_elim (h : False) : (0 : Nat) = 1 := by','exact (by exact False.elim h)',True),
        ('theorem no_local_assumption : False := by','exact (by assumption)',False)]
for header,body,expected in checks:
    r=kernel.verify_claim(claim={'lean_proof':body},target=header)['verifier_result']
    assert r['returncode'] in (0,1) and r['success']==expected,r
print('PASS: real Lean accepts valid assumption/False.elim, rejects assumption without a hypothesis; declaration-specific axiom audit intact.',flush=True)
