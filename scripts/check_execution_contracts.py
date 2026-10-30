"""Offline fault injection: invalid proof outputs consume attempts; infrastructure errors never become proof scores."""
from pathlib import Path
import json,os,sys,tempfile
from types import SimpleNamespace as NS
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
os.environ['LANGSMITH_TRACING']='false'
from single_orchestrator.goal_search import GoalSearch
from single_orchestrator.goal_api import complete_with_retries,GoalProtocolError,GoalEnvironmentError,GoalTransportError
from single_orchestrator.memory import JsonlTrialMemory

def response(raw):return NS(choices=[NS(finish_reason='stop',message=NS(content=raw))])
class Client:
 def __init__(self,fault=None,recover=False):self.fault=fault;self.recover=recover;self.calls=[];self.api=[]
 def complete(self,p):
  self.calls.append(p);i=p.inputs;stage='plan' if 'strategy' in p.name else 'natural' if 'nature_agent' in p.name else 'lean' if 'lean_generator' in p.name else 'repair'
  task=i.get('assigned_task',i.get('task','prove'))
  valid={'plan':json.dumps({'task':task,'lean_strategy':'lean strategy'}),'natural':'An elementary mathematical argument.','lean':json.dumps({'lean_proof':['exact unknown_lemma']}),'repair':json.dumps({'retry_guidance':'Before: exact unknown_lemma; After: another_attempt'})}[stage]
  bad={'plan':json.dumps({'task':'invalid','strategy':'math','lean_strategy':'lean'}),'natural':'```lean\nexact h\n```','lean':json.dumps({'lean_proof':['theorem changed_goal : True := by trivial']}),'repair':'{}'}[stage]
  seq=iter([bad,valid]) if self.recover and stage==self.fault else None
  def request():return response(next(seq) if seq else bad if stage==self.fault else valid)
  return complete_with_retries(p,request,max_attempts=2,on_attempt=lambda *x:self.api.append(x),wait=lambda _:None)[0]
class Verifier:
 prelude=''
 def __init__(self,environment=False):self.calls=[];self.environment=environment
 def verify(self,source,formal_statement):
  self.calls.append(source)
  return {'success':False,'returncode':None if self.environment else 1,'timed_out':self.environment,'formal_state':source,'lean_error_log':'Compiler wall-clock timeout' if self.environment else 'unknown identifier unknown_lemma'}
def engine(d,c,v,mode='single',rounds=1):
 return GoalSearch(model_client=c,verifier=v,memories={t:JsonlTrialMemory(d/(t+'.jsonl')) for t in ('prove','disprove')},mode=mode,max_trials=3,max_rounds=rounds)
checks=[]
with tempfile.TemporaryDirectory() as temp:
 root=Path(temp)
 for mode in ('single','dual'):
  for stage in ('plan','natural','lean','repair'):
   d=root/(mode+stage);d.mkdir();c=Client(stage);v=Verifier()
   if stage=='plan':
    try:engine(d,c,v,mode).run(problem='Public problem',formal_statement='theorem contract_fixture : (2 : Nat) = 3 := by')
    except GoalProtocolError:pass
    else:raise AssertionError('Planner violation created a default strategy')
    assert not v.calls
   else:
    r=engine(d,c,v,mode).run(problem='Public problem',formal_statement='theorem contract_fixture : (2 : Nat) = 3 := by')
    expected=3 if mode=='single' else 6
    assert r['final_status']=='not_solved' and r['trials']==expected and len(r['failed_attempts'])==expected
    assert len(v.calls)==(expected if stage=='repair' else 0)
   assert 1 <= len([p for p in c.calls if 'strategy' in p.name]) <= (2 if mode=='dual' else 1)
   checks.append(mode+' '+stage+': invalid output rejected; no default, worker faults consume only the attempt')
  for stage in ('plan','natural','lean','repair'):
   d=root/(mode+'recovered'+stage);d.mkdir();c=Client(stage,recover=True);v=Verifier();r=engine(d,c,v,mode).run(problem='Public',formal_statement='theorem recover_fixture : (2 : Nat) = 3 := by')
   expected=3 if mode=='single' else 6
   assert r['final_status']=='not_solved' and len(v.calls)==expected and r['trials']==expected
   assert sum('nature_agent' in p.name for p in c.calls)==(1 if mode=='single' else 2)
   for p in c.calls:
    if 'nature_agent' in p.name:assert not {'strategy','lean_strategy'} & p.inputs.keys()
    if 'lean_generator' in p.name:
     assert p.inputs['informal']=='An elementary mathematical argument.' and p.inputs['lean_strategy']=='lean strategy'
     if 'previous_lean_proof' in p.inputs:assert p.inputs['repair_guidance'].startswith('Before:')
   checks.append(mode+' '+stage+': recovery stays within node; ordinary proof failures retain all attempts')
 for mode in ('single','dual'):
  d=root/('environment'+mode);d.mkdir();v=Verifier(environment=True);c=Client()
  try:engine(d,c,v,mode).run(problem='Public',formal_statement='theorem env_fixture : False := by')
  except GoalEnvironmentError:pass
  else:raise AssertionError('Environment timeout scored as model failure')
  assert len(v.calls)==1 and not any('retry_planner' in p.name for p in c.calls)
  checks.append(mode+': environment failure aborts before Repair')
 # Direct adapters bypassing the HTTP helper must also fail closed on node errors.
 class Broken(Client):
  def complete(self,p):
   if 'strategy' in p.name:return '{'
   return super().complete(p)
 d=root/'direct_adapter';d.mkdir()
 try:engine(d,Broken(),Verifier(),'dual').run(problem='Public',formal_statement='theorem direct_fixture : False := by')
 except GoalProtocolError:pass
 else:raise AssertionError('Malformed direct adapter response created a default plan')
 checks.append('direct adapter parse failure cannot create a fallback strategy')
 # Exactly fixed direction, strict output fields, nonempty tactic body, Natural prose.
 for name,inputs,raw in [
  ('dual_orchestrator.truth_strategy',{'assigned_task':'prove'},'{"task":"disprove","strategy":"x","lean_strategy":"y"}'),
  ('lean_generator.generate',{},'{"lean_proof":["by"]}'),
  ('lean_generator.generate',{},'{"lean_proof":["rfl"],"goal":"True"}'),
  ('lean_generator.generate',{},'{"abandon":true,"reason":"opposite"}'),
  ('nature_agent.initial',{},'ABANDON: I cannot prove it'),
  ('nature_agent.initial',{},'theorem fake : True := by trivial')]:
  p=NS(name=name,inputs=inputs,version='v18-format-repair-lines')
  try:complete_with_retries(p,lambda:response(raw),max_attempts=1,wait=lambda _:None)
  except GoalProtocolError:pass
  else:raise AssertionError(('invalid contract accepted',name,raw))
 checks.append('wrong direction, goal override, late abandonment, empty proof and Natural Lean output rejected')
 # A wrong but correctly packaged Lean argument is allowed to reach the real verifier.
 p=NS(name='lean_generator.generate',inputs={},version='v18-format-repair-lines')
 complete_with_retries(p,lambda:response('{"lean_proof":["exact definitely_wrong_lemma"]}'),wait=lambda _:None)
 checks.append('mathematically incorrect well-packaged code is a proof-performance issue, not a protocol violation')
( ROOT/'.runs/execution_contract_checks_20261015').mkdir(parents=True,exist_ok=True)
(ROOT/'.runs/execution_contract_checks_20261015/summary.json').write_text(json.dumps({'checks':checks,'paid_model_calls':0,'real_lean_builds':0,'verification':'fault-injection tests only'},ensure_ascii=False,indent=2)+'\n')
print(f'PASS: {len(checks)} execution-contract checks; zero paid model calls.')
