"""No-network regression tests for the observed API and diagnostic failures."""
from pathlib import Path
import json,sys
from types import SimpleNamespace as NS
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from single_orchestrator.goal_api import complete_with_retries,response_format,GoalOutputError,GoalProtocolError
from single_orchestrator.lean_diagnostics import grounded_summary

def response(text,finish='stop'):
 return NS(choices=[NS(finish_reason=finish,message=NS(content=text))])
p=NS(name='dual_orchestrator.falsity_strategy',inputs={'assigned_task':'disprove'})
valid=json.dumps({'task':'disprove','strategy':'Find a counterexample','lean_strategy':'Construct the witness'})
outputs=iter([response(None,'error'),response(valid)]);attempts=[]
raw,_=complete_with_retries(p,lambda:next(outputs),on_attempt=lambda n,r,e:attempts.append((n,str(e) if e else None)),wait=lambda _:None)
assert raw==valid and len(attempts)==2 and attempts[-1][1] is None
assert response_format(p)['json_schema']['schema']['required']==['task','strategy','lean_strategy']
# Optional abandonment remains legal and is never schema-forced onto normal work.
q=NS(name='truth_lean_generator.generate',inputs={})
assert response_format(q)=={'type':'json_object'}
for text in ('{"lean_proof":"trivial"}','{"abandon":true,"reason":"opposite direction holds"}'):
 complete_with_retries(q,lambda:response(text),wait=lambda _:None)
# Current policy rejects late abandonment as an output fault, then requests code/guidance.
for name,valid_text in [('truth_lean_generator.generate','{"lean_proof":"trivial"}'),
                        ('falsity_retry_planner.plan','{"retry_guidance":"Fix Lean code","lean_error_summary":"type mismatch"}')]:
 current=NS(name=name,inputs={},version='v18-natural-only-abandonment')
 seq=iter([response('{"abandon":true,"reason":"The original statement is true."}'),response(valid_text)])
 recorded=[]
 raw,_=complete_with_retries(current,lambda:next(seq),on_attempt=lambda *x:recorded.append(x),wait=lambda _:None)
 assert raw==valid_text and len(recorded)==2 and 'only at the Natural stage' in str(recorded[0][2])
# Terminal faults must exhaust bounded retries, never create a fake successful plan.
attempts=[]
try:complete_with_retries(p,lambda:response(None,'error'),on_attempt=lambda *x:attempts.append(x),wait=lambda _:None)
except GoalOutputError:pass
else:raise AssertionError('Provider failure was accepted')
assert len(attempts)==3
# A bad lemma signature cannot enter planner memory as a compiler fact.
log='Scratch.lean:15:8: error: Function expected at\n  IsConnected.closure hA\nbut this term has type\n  IsConnected (closure A)\n\nNote: Expected a function because this term is being applied to an argument.'
summary,source=grounded_summary('The lemma requires a closed set as its first argument.',log)
assert source=='compiler_excerpt' and 'requires a closed' not in summary and 'IsConnected (closure A)' in summary
assert grounded_summary('Function expected at',log)==('Function expected at','repair_quote')
assert 'Scratch.lean' not in summary and 'Note:' not in summary
print('PASS: API recovery/exhaustion, shared optional abandonment, and compiler-grounded summaries; zero API calls.')

class RateLimitError(Exception):
 status_code=429
 response=NS(headers={'retry-after':'23'})
sequence=iter([RateLimitError('temporary rate limit'),response(valid)])
def request():
 value=next(sequence)
 if isinstance(value,Exception):raise value
 return value
waits=[]
complete_with_retries(p,request,wait=waits.append)
assert waits==[23],waits
print('PASS: HTTP429 respects Retry-After; no actual wait or network request.')

# Infrastructure retries must not consume the three output-recovery opportunities.
from single_orchestrator.goal_api import GoalTransportError
seq=iter([response('{}'),RateLimitError('temporary rate limit'),response('{}'),response(valid)])
def mixed_request():
 x=next(seq)
 if isinstance(x,Exception):raise x
 return x
records=[]
raw,_=complete_with_retries(p,mixed_request,on_attempt=lambda *x:records.append(x),wait=lambda _:None)
assert raw==valid and len(records)==4 and [x[0] for x in records]==[1,2,3,4]
# Terminal infrastructure errors escape the orchestrator instead of becoming a plan/fallback.
records=[]
try:
 complete_with_retries(p,lambda:(_ for _ in ()).throw(RateLimitError('busy')),
                       max_transport_attempts=2,on_attempt=lambda *x:records.append(x),wait=lambda _:None)
except GoalTransportError:pass
else:raise AssertionError('Infrastructure exhaustion was scored as a model response')
assert len(records)==2
from single_orchestrator.goal_search import GoalSearch
class UnavailableClient:
 def complete(self,prompt):raise GoalTransportError('provider unavailable')
g=GoalSearch.__new__(GoalSearch);g.model_client=UnavailableClient();g.mode='single'
for task in (None,'prove','disprove'):
 try:g.plan('Public problem',{'prove':'P','disprove':'not P'},[],1,forced_task=task)
 except GoalTransportError:pass
 else:raise AssertionError('Infrastructure failure became a fallback strategy')
# A long provider cooldown is honored in <=60s wait chunks, with zero real waits.
class LongCooldown(RateLimitError):response=NS(headers={'Retry-After':'125'})
seq=iter([LongCooldown('busy'),response(valid)]);waits=[]
def cooled_request():
 x=next(seq)
 if isinstance(x,Exception):raise x
 return x
complete_with_retries(p,cooled_request,wait=waits.append)
assert waits==[60,60,5],waits
print('PASS: separate transport/output budgets, fatal infrastructure propagation, full Retry-After cooldown; zero API calls.')

# Current Lean/Repair requests enforce their exact fields at the provider too.
for name,fields in [('truth_lean_generator.generate',['lean_proof']),('falsity_retry_planner.plan',['retry_guidance','lean_error_summary'])]:
 fmt=response_format(NS(name=name,version='v18-natural-only-abandonment'))
 assert fmt['type']=='json_schema' and fmt['json_schema']['strict']
 assert fmt['json_schema']['schema']['required']==fields
 assert fmt['json_schema']['schema']['additionalProperties'] is False
print('PASS: strict current Lean/Repair output schemas, historical JSON-object behavior preserved.')

# Line-array transport avoids embedded line-break escaping in proof strings.
lines=NS(name='lean_generator.generate',inputs={},version='v18-format-repair-lines')
assert response_format(lines)['json_schema']['schema']['properties']['lean_proof']['type']=='array'
complete_with_retries(lines,lambda:response('{"lean_proof":["intro h","exact h"]}'),wait=lambda _:None)
try:complete_with_retries(lines,lambda:response('{"lean_proof":"by"}'),max_attempts=1,wait=lambda _:None)
except GoalProtocolError:pass
else:raise AssertionError('String proof bypassed line-array output contract')
print('PASS: line-array proof API contract and rejection of ambiguous string output.')

# Lean-only planner output must agree at schema, validation, and recovery stages.
from single_orchestrator.goal_api import output_correction
current=NS(name='orchestrator.strategy',inputs={},version='v19-lean-strategy-only')
schema=response_format(current)['json_schema']['schema']
assert schema['required']==['task','lean_strategy'] and 'strategy' not in schema['properties']
lean_plan='{"task":"prove","lean_strategy":"Introduce the variables and use extensionality."}'
seq=iter([response('{"task":"prove","strategy":"Tell Natural what to do","lean_strategy":"ext"}'),response(lean_plan)])
records=[]
raw,_=complete_with_retries(current,lambda:next(seq),on_attempt=lambda *x:records.append(x),wait=lambda _:None)
assert raw==lean_plan and len(records)==2
assert 'task and lean_strategy' in output_correction(current,records[0][2])
for node in ('lean_generator.generate','truth_lean_generator.generate'):
 proof=NS(name=node,inputs={},version=current.version)
 assert response_format(proof)['json_schema']['schema']['properties']['lean_proof']['type']=='array'
 try:complete_with_retries(proof,lambda:response('{"lean_proof":["sorry"]}'),max_attempts=1,wait=lambda _:None)
 except GoalProtocolError:pass
 else:raise AssertionError('Lean-only planner version lost proof validation')
print('PASS: v19 Lean-only planner schema, extra-field rejection/recovery, and proof guards; zero API calls.')
