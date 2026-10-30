"""Fault injection only: no paid models and no real Lean verification."""
from pathlib import Path
from threading import Event,Lock
import importlib.util,json,os,sys,tempfile
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'.runs/lane_isolation_checks_20261017';OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT/'src'));os.environ['LANGSMITH_TRACING']='false';os.environ['DUAL_ARC_LEAN_DECLARATIONS']='0'
from single_orchestrator.goal_api import GoalProtocolError,GoalEnvironmentError,GoalTransportError
from single_orchestrator.memory import JsonlTrialMemory
from single_orchestrator.goal_search import GoalSearch
class Client:
 def __init__(self,mode='dual',fault_stage='lean',fault_task='prove',fault_rounds=(1,),fault_trial=1,transport=False):
  self.mode=mode;self.fault_stage=fault_stage;self.fault_task=fault_task;self.fault_rounds=fault_rounds;self.fault_trial=fault_trial;self.transport=transport;self.calls=[];self.rounds={};self.trials={};self.failed=Event();self.guard=Lock()
 def complete(self,p):
  with self.guard:
   self.calls.append(p);i=p.inputs;stage='plan' if 'strategy' in p.name else 'natural' if 'nature_agent' in p.name else 'lean' if 'lean_generator' in p.name else 'repair';task=i.get('assigned_task',i.get('task','prove'))
   if stage=='plan':self.rounds[task]=self.rounds.get(task,0)+1;self.trials[task]=0
   if stage=='lean':self.trials[task]+=1
   rd=self.rounds[task];tr=self.trials[task]
  if self.transport:raise GoalTransportError('Injected infrastructure failure')
  if stage==self.fault_stage and (self.fault_task=='both' or task==self.fault_task) and rd in self.fault_rounds and (stage in ('plan','natural') or tr==self.fault_trial):
   self.failed.set();raise GoalProtocolError('Injected terminal invalid output, no replacement')
  if self.mode=='dual' and stage=='lean' and task=='disprove' and rd==1 and 1 in self.fault_rounds and self.fault_stage=='lean' and self.fault_trial==1:
   assert self.failed.wait(5)
  if stage=='plan':return json.dumps({'task':task,'lean_strategy':'Implement the exact assigned goal.'})
  if stage=='natural':return 'An independent attempted mathematical argument.'
  if stage=='lean':return json.dumps({'lean_proof':['trivial']})
  return json.dumps({'retry_guidance':'Use a concrete supported correction.'})
class Verifier:
 prelude=''
 def __init__(self,c,winner='disprove',win_round=1,win_trial=2,environment=False,precheck=False):self.c=c;self.winner=winner;self.win_round=win_round;self.win_trial=win_trial;self.environment=environment;self.precheck=precheck;self.calls=[]
 def verify(self,source,formal_statement=''):
  task='disprove' if ' : ¬ ' in formal_statement else 'prove';self.calls.append(task)
  if self.environment:return {'success':False,'returncode':None,'timed_out':True,'formal_state':source,'lean_error_log':'Compiler timeout'}
  if self.precheck and task=='prove':return {'success':False,'returncode':None,'timed_out':False,'formal_state':source,'lean_error_log':'Legal tactic blocked by precheck'}
  ok=task==self.winner and self.c.rounds[task]>=self.win_round and self.c.trials[task]>=self.win_trial
  return {'success':ok,'returncode':0 if ok else 1,'timed_out':False,'formal_state':source,'lean_error_log':f"'{formal_statement.split()[1]}' depends on axioms: [propext]" if ok else 'ordinary Lean error'}
def run(c,v,mode='dual',rounds=3):
 with tempfile.TemporaryDirectory() as tmp:
  e=GoalSearch(model_client=c,verifier=v,mode=mode,max_trials=3,max_rounds=rounds,memories={t:JsonlTrialMemory(Path(tmp)/(t+'.jsonl')) for t in ('prove','disprove')})
  return e.run(problem='Public fixture only',formal_statement='theorem isolation_fixture : (2 : ℕ) = 3 := by')
checks=[]
for mode in ('single','dual'):
 c=Client(mode=mode);v=Verifier(c,winner='prove');r=run(c,v,mode)
 assert r['final_status']=='solved' and r['rounds']==1 and r['proof_trials']==2
 assert len(r['failed_attempts'])==1 and r['failed_attempts'][0]['trial']==1
 assert next(t for t in r['trial_memory'] if t['verifier_result']['success'])['trial_index']==2
 assert sum('nature_agent' in p.name and p.inputs['task']=='prove' for p in c.calls)==1
 checks.append(mode+': Generator T1 error consumes T1; the same lane succeeds at R1T2 with fixed Natural/strategy')
c=Client();v=Verifier(c);r=run(c,v)
assert r['final_status']=='false' and r['rounds']==1 and len(r['failed_attempts'])==1
checks.append('an early sibling output error cannot cancel the healthy lane')
class OnceNatural(Client):
 def __init__(self):super().__init__(mode='single',fault_rounds=());self.seen=False
 def complete(self,p):
  raw=super().complete(p)
  if 'nature_agent' in p.name and not self.seen:
   self.seen=True;raise GoalProtocolError('Injected invalid Natural output')
  return raw
c=OnceNatural();v=Verifier(c,winner='prove',win_trial=1);r=run(c,v,'single')
assert r['final_status']=='solved' and r['rounds']==1 and r['trials']==2
assert sum('nature_agent' in p.name for p in c.calls)==2
assert r['trial_memory'][0]['trial_index']==2
checks.append('invalid Natural consumes T1 and is retried at T2, without inventing an argument')
c=Client(mode='single',fault_stage='repair');v=Verifier(c,winner='prove');r=run(c,v,'single')
assert r['final_status']=='solved' and r['rounds']==1 and r['trials']==len(v.calls)==2
assert len(r['failed_attempts'])==1 and len(r['trial_memory'])==2 and r['trial_memory'][0]['retry_plan']==''
assert sum('nature_agent' in p.name for p in c.calls)==1
retry=[p for p in c.calls if 'lean_generator' in p.name][-1]
assert 'previous_lean_proof' in retry.inputs and 'repair_guidance' not in retry.inputs
checks.append('Repair T1 error preserves the build, consumes no extra attempt, and T2 proceeds without fake guidance')
class MalformedRepair(Client):
 def complete(self,p):
  raw=super().complete(p)
  if 'retry_planner' in p.name:return '{}'
  return raw
c=MalformedRepair(mode='single',fault_rounds=());v=Verifier(c,winner='prove');r=run(c,v,'single')
assert r['final_status']=='solved' and r['rounds']==1 and r['trials']==2 and r['trial_memory'][0]['retry_plan']==''
checks.append('malformed Repair JSON is also confined to its attempt')
class OncePrecheck(Verifier):
 def __init__(self,c):super().__init__(c,winner='prove');self.first=True
 def verify(self,source,formal_statement=''):
  if self.first:
   self.first=False;self.calls.append('prove')
   return {'success':False,'returncode':None,'timed_out':False,'formal_state':source,'lean_error_log':'Injected pre-compilation rejection'}
  return super().verify(source,formal_statement)
c=Client(mode='single',fault_rounds=());v=OncePrecheck(c);r=run(c,v,'single')
assert r['final_status']=='solved' and r['rounds']==1 and r['trials']==2 and len(r['failed_attempts'])==1
checks.append('pre-compilation rejection consumes T1 only; rejected code is never a successful proof')
for mode in ('single','dual'):
 for stage in ('natural','lean','repair'):
  c=Client(mode=mode,fault_stage=stage,fault_task='both',fault_rounds=(1,2,3));v=Verifier(c,winner=None)
  # All three Generator/Repair attempts fail, not just the first one.
  if stage!='natural':
   old=c.complete
   def fail_every_trial(p,old=old,c=c,stage=stage):
    if (stage=='lean' and 'lean_generator' in p.name) or (stage=='repair' and 'retry_planner' in p.name):
     c.trials[p.inputs['task']]+=1 if stage=='lean' else 0
     raise GoalProtocolError('Injected invalid worker output')
    return old(p)
   c.complete=fail_every_trial
  r=run(c,v,mode);expected=9 if mode=='single' else 18
  assert r['final_status']=='not_solved' and r['rounds']==3 and r['trials']==expected and len(r['failed_attempts'])==expected
  assert len(v.calls)==(expected if stage=='repair' else 0)
  checks.append(mode+' '+stage+': every invalid output counts one failed attempt; full 3x3 cap is respected')
for mode in ('single','dual'):
 c=Client(mode=mode,fault_rounds=());v=Verifier(c,environment=True)
 try:run(c,v,mode)
 except GoalEnvironmentError:pass
 else:raise AssertionError('Environment failure was scored as a failed code attempt')
 assert len(v.calls)==1 and not any('retry_planner' in p.name for p in c.calls)
 checks.append(mode+': actual compiler environment failure still aborts')
 c=Client(mode=mode,transport=True);v=Verifier(c)
 try:run(c,v,mode)
 except GoalTransportError:pass
 else:raise AssertionError('Transport failure was hidden')
 assert not v.calls
 checks.append(mode+': transport failure still aborts')
from single_orchestrator.goal_search import _lane_stop
class PendingAfterFault(Client):
 def __init__(self):super().__init__();self.pending=Event()
 def complete(self,p):
  raw=super().complete(p)
  if 'lean_generator' in p.name:
   task=p.inputs['task']
   if task=='prove' and self.trials[task]==2:
    self.pending.set();assert _lane_stop.get().wait(5)
   elif task=='disprove':assert self.pending.wait(5)
  return raw
c=PendingAfterFault();v=Verifier(c,win_trial=1);r=run(c,v)
assert r['final_status']=='false' and r['trials']==2 and len(r['failed_attempts'])==1 and v.calls==['disprove']
checks.append('a prior failed attempt remains counted when sibling success cancels the next in-flight attempt')
for mode in ('single','dual'):
 c=Client(mode=mode,fault_rounds=());v=Verifier(c,winner=None);r=run(c,v,mode)
 assert r['final_status']=='not_solved' and r['trials']==(9 if mode=='single' else 18) and not r['failed_attempts']
 checks.append(mode+': ordinary Lean failures retain the original budget')
summary={'checks':checks,'paid_model_calls':0,'real_lean_builds':0,'scope':'Attempt-level fault injection, not an accuracy result.'}
(OUT/'attempt_checks.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n');print('PASS:',len(checks),'attempt-isolation checks; zero model/real Lean calls.')
