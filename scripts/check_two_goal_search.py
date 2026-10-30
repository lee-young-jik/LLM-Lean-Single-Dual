"""No-API checks for task selection, fixed lanes, and real Lean decisions."""
from pathlib import Path
import json,os,sys,tempfile
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from single_orchestrator.goal_search import GoalSearch,fixed_goals,GoalVerifier
from single_orchestrator.runtime import SingleOrchestrator
from single_orchestrator.dual_runtime import DualOrchestrator
from single_orchestrator.memory import JsonlTrialMemory
from single_orchestrator.verifier import LakeVerifier
os.environ['DUAL_ARC_SEARCH_ARCHITECTURE']='two_goal'
class Client:
 def __init__(self,tasks=('prove','disprove')):self.tasks=list(tasks);self.calls=[];self.plans=0
 def complete(self,p):
  self.calls.append(p);x=p.inputs
  assert 'PRIVATE_KEY' not in p.text
  if any(node in p.name for node in ('nature_agent','lean_generator','retry_planner')):
   assert x['goal'] and not {'original_goal','goals'} & x.keys()
   assert 'prove the selected goal exactly as supplied' in p.text
   assert 'do not negate or refute it again' in p.text
  if 'strategy' in p.name or p.name=='orchestrator.restrategy':
   if 'assigned_task' in x:
    assert x['goal'] and x['problem'] and not {'goals','original_goal'} & x.keys()
   else:
    assert set(x['goals'])=={'prove','disprove'} and x['problem']
   t=x.get('assigned_task') or self.tasks[min(self.plans,len(self.tasks)-1)];self.plans+=1
   return json.dumps({'task':t,'lean_strategy':'Use norm_num with the fixed goal.'})
  if 'lean_generator' in p.name:
   t=x['task'];body='norm_num' if t=='prove' else 'intro h\nnorm_num at h'
   return json.dumps({'lean_proof':body})
  if 'retry_planner' in p.name:
   return json.dumps({'retry_guidance':'mathematical reasoning or repair'})
  return 'mathematical reasoning or repair'
class Verifier:
 prelude=''
 def verify(self,source,formal_statement):
  name=formal_statement.split()[1];truth=('(2 : ℕ) = 2' in formal_statement)
  neg=' : ¬ ' in formal_statement;valid=(truth and not neg) or (not truth and neg)
  return {'success':valid,'returncode':0 if valid else 1,'formal_state':source,
          'lean_error_log':f"'{name}' depends on axioms: [propext]" if valid else 'ordinary Lean error','timed_out':False}
with tempfile.TemporaryDirectory() as t:
 d=Path(t);m=JsonlTrialMemory(d/'single.jsonl');c=Client()
 single=SingleOrchestrator(model_client=c,verifier=Verifier(),memory=m,max_trials=1,max_strategy_rounds=2)
 r=single.run('PUBLIC_PROBLEM',formal_statement='theorem switch_test : (2 : ℕ) = 3 := by',expected_false_transform='PRIVATE_KEY')
 assert r['final_status']=='false' and r['rounds']==2
 assert [x['task'] for x in r['trial_memory']]==['prove','disprove']
 assert len([x for x in c.calls if 'lean_generator' in x.name])==2
 c=Client();dual=DualOrchestrator(model_client=c,verifier=Verifier(),truth_memory=JsonlTrialMemory(d/'truth.jsonl'),falsity_memory=JsonlTrialMemory(d/'false.jsonl'),max_trials=1,max_rounds=1)
 r=dual.run('PUBLIC_PROBLEM',formal_statement='theorem both_test : (2 : ℕ) = 2 := by',expected_false_transform='PRIVATE_KEY')
 assert r['final_status']=='solved'
 assert r['proof_trials']==1 and r['disproof_trials']<=1, 'Verified proof must stop further sibling trials'
 assert 1 <= len([x for x in c.calls if 'lean_generator' in x.name]) <= 2
 assert r['dual_truth_trials']==1 and r['dual_falsity_trials']<=1
 for prompt in c.calls:
  if 'strategy' in prompt.name:
   assert prompt.inputs['goal']==r['goals'][prompt.inputs['assigned_task']]
   assert set(prompt.inputs)=={'problem','goal','assigned_task'}
   assert prompt.inputs['problem']=='PUBLIC_PROBLEM'
   assert 'goals.prove' not in prompt.text and 'goals.disprove' not in prompt.text
 assert not any(x.name in ('false_reason','false_check','orchestrator.false_decision') for x in c.calls)
 assert '∀ (x : ℕ) (h : 0 < x)' in fixed_goals('theorem quantified (x : ℕ) (h : 0 < x) : x = x := by')['disprove']
# Shared lane instructions, with an explicit Dual-only abandonment protocol.
with tempfile.TemporaryDirectory() as t:
 captured = {}
 for mode in ('single', 'dual'):
  client = Client()
  memory = JsonlTrialMemory(Path(t) / f'{mode}.jsonl')
  engine = GoalSearch(model_client=client, verifier=Verifier(),
      memories={'prove': memory, 'disprove': memory}, mode=mode,
      max_trials=2, max_rounds=1)
  engine.lane('prove',
      fixed_goals('theorem retry_test : (2 : ℕ) = 3 := by'),
      'PUBLIC_PROBLEM', [], 1, lean_strategy='Use norm_num with the fixed goal.')
  nature_calls=[p for p in client.calls if 'nature_agent' in p.name]
  assert len(nature_calls)==1 and nature_calls[0].name.endswith('.initial')
  retry_lean=[p for p in client.calls if 'lean_generator' in p.name][1]
  assert retry_lean.inputs['previous_lean_proof']=='norm_num'
  assert retry_lean.inputs['lean_error_log']=='ordinary Lean error'
  assert retry_lean.inputs['repair_guidance']=='mathematical reasoning or repair'
  assert 'trial_memory' not in retry_lean.inputs and 'goals' not in retry_lean.inputs
  captured[mode] = []
  for prompt in client.calls:
   inputs = dict(prompt.inputs)
   role = inputs.pop('role_assumption', None)
   if 'nature_agent' in prompt.name or 'lean_generator' in prompt.name:
    assert (role is not None) == (mode == 'dual')
    if role is not None:
     assert 'Your fixed task is prove' in role
   instructions=prompt.text.split('\n\nUser:')[0]
   if mode=='dual':
    from single_orchestrator import goal_prompts
    for extra in (goal_prompts.DUAL_NATURAL_CONTINUE,goal_prompts.DUAL_LEAN_CONTINUE,goal_prompts.DUAL_REPAIR_CONTINUE):
     instructions=instructions.replace('\n'+extra,'')
   captured[mode].append((instructions, inputs))
 assert captured['single'] == captured['dual'], 'Shared lane instructions differ beyond Dual roles and abandonment'
from single_orchestrator import goal_prompts
from single_orchestrator.planner_memory import planner_memory
source_record={'task':'prove','strategy_round':1,'trial_index':1,'strategy':'first approach',
               'informal':'argument','lean_code':'example : True := by trivial',
               'lean_error_log':'compiler detail','retry_plan':'repair detail','private_label':'secret'}
projected=planner_memory([source_record], {})
assert len(projected)==1 and projected[0]['strategy']=='first approach'
assert not {'lean_error_log','retry_plan','private_label'} & projected[0].keys()
assert source_record['retry_plan']=='repair detail'
# Round/direction strategy is shared once; all three attempts remain intact.
from copy import deepcopy
history=[]
for round_index, task in ((1,'prove'), (1,'disprove'), (2,'prove')):
 for trial in (1,2,3):
  history.append({**source_record,'strategy_round':round_index,'task':task,
                  'trial_index':trial,'informal':f'{round_index}/{task}'})
original=deepcopy(history)
grouped=planner_memory(history)
assert len(grouped)==3 and history==original
assert [(g['round'],g['task']) for g in grouped]==[(1,'prove'),(1,'disprove'),(2,'prove')]
for group in grouped:
 assert set(group)=={'round','task','strategy','informal','trials'}
 assert group['informal']==f"{group['round']}/{group['task']}"
 assert [t['trial'] for t in group['trials']]==[1,2,3]
 assert all(set(t)=={'trial','lean_code'} for t in group['trials'])
assert planner_memory(grouped)==grouped, 'Reprojection must not drop grouped trials'
with tempfile.TemporaryDirectory() as t:
 for mode in ('single','dual'):
  client=Client();memory=JsonlTrialMemory(Path(t)/f'{mode}.jsonl')
  engine=GoalSearch(model_client=client,verifier=Verifier(),
      memories={'prove':memory,'disprove':memory},mode=mode)
  engine.plan('PUBLIC_PROBLEM',fixed_goals('theorem memory_test : True := by'),
      history,3,forced_task='prove' if mode=='dual' else None)
  assert client.calls[-1].inputs['trial_memory']==grouped
  assert client.calls[-1].version=='v21-guidance-only-repair'
print('PASS: shared strategies once per round/direction; all trials retained in both planner inputs.')
# Repair has two outputs; only its error summary reaches next-round planning.
class AlwaysFailVerifier(Verifier):
 def verify(self,source,formal_statement):
  return {'success':False,'returncode':1,'formal_state':source,
          'lean_error_log':'RAW_COMPILER_DETAIL\nerror: type mismatch',
          'timed_out':False}
class StructuredRepairClient(Client):
 def complete(self,p):
  if 'retry_planner' in p.name:
   self.calls.append(p)
   return json.dumps({'retry_guidance':'RETRY_GUIDANCE_ONLY: improve the mathematical argument'})
  return super().complete(p)
with tempfile.TemporaryDirectory() as t:
 for mode in ('single','dual'):
  client=StructuredRepairClient(tasks=('prove',));directory=Path(t)/mode;directory.mkdir()
  engine=GoalSearch(model_client=client,verifier=AlwaysFailVerifier(),
      memories={task:JsonlTrialMemory(directory/(task+'.jsonl')) for task in ('prove','disprove')},
      mode=mode,max_trials=3,max_rounds=2)
  result=engine.run(problem='PUBLIC_PROBLEM',formal_statement='theorem error_memory : (2 : ℕ) = 3 := by')
  repair_calls=[p for p in client.calls if 'retry_planner' in p.name]
  nature_calls=[p for p in client.calls if 'nature_agent' in p.name]
  lean_calls=[p for p in client.calls if 'lean_generator' in p.name]
  assert len(nature_calls)==(2 if mode=='single' else 4), 'One natural argument per round/direction'
  assert len(lean_calls)==(6 if mode=='single' else 12), 'Three Lean attempts per round/direction'
  assert not any('.retry' in p.name for p in nature_calls)
  for call in lean_calls:
   assert call.inputs['informal']=='mathematical reasoning or repair'
  assert len(repair_calls)==(6 if mode=='single' else 12), 'Failed attempts retain retry guidance only'
  assert all('lean_error_summary' not in r for r in result['trial_memory'])
  assert all(r['lean_strategy']=='Use norm_num with the fixed goal.' and 'strategy' not in r for r in result['trial_memory'])
  later_plans=[p for p in client.calls if 'strategy' in p.name and p.inputs.get('trial_memory')]
  assert len(later_plans)==(1 if mode=='single' else 2)
  for plan in later_plans:
   assert all(g['lean_strategy']=='Use norm_num with the fixed goal.' for g in plan.inputs['trial_memory'])
   assert 'RAW_COMPILER_DETAIL' not in json.dumps(plan.inputs)
   assert 'RETRY_GUIDANCE_ONLY' not in json.dumps(plan.inputs)
   assert all(not {'lean_error_summary','lean_error_log','retry_plan','repair_guidance'} & a.keys()
              for g in plan.inputs['trial_memory'] for a in g['trials'])
   assert planner_memory(plan.inputs['trial_memory'])==plan.inputs['trial_memory']
  if mode=='dual':assert later_plans[0].inputs['trial_memory']==later_plans[1].inputs['trial_memory']
  for p in client.calls:
   if 'nature_agent' in p.name:
    assert not {'strategy','lean_strategy','trial_memory'} & p.inputs.keys()
    assert p.inputs['problem']=='PUBLIC_PROBLEM'
   if 'lean_generator' in p.name:
    assert p.inputs['lean_strategy']=='Use norm_num with the fixed goal.'
   assert 'nature_agent.retry' not in p.name
   if 'lean_generator' in p.name and 'previous_lean_proof' in p.inputs:
    assert 'RAW_COMPILER_DETAIL' in p.inputs['lean_error_log']

 class MalformedRepairClient(Client):
  def complete(self,p):
   if 'retry_planner' in p.name:
    self.calls.append(p);return 'invalid repair JSON'
   return super().complete(p)
 client=MalformedRepairClient(tasks=('prove',));directory=Path(t)/'bad_repair';directory.mkdir()
 engine=GoalSearch(model_client=client,verifier=AlwaysFailVerifier(),
     memories={task:JsonlTrialMemory(directory/(task+'.jsonl')) for task in ('prove','disprove')},
     mode='single',max_trials=1,max_rounds=2)
 from single_orchestrator.goal_api import GoalProtocolError
 result=engine.run(problem='PUBLIC_PROBLEM',formal_statement='theorem malformed_repair : (2 : ℕ) = 3 := by')
 assert result['final_status']=='not_solved' and result['trials']==2
 assert len(result['failed_attempts'])==2
 assert all(not r['retry_plan'] for r in result['trial_memory'])
 assert len([p for p in client.calls if 'strategy' in p.name])==2
 assert not any('previous_lean_proof' in p.inputs for p in client.calls)

from single_orchestrator.goal_trace import node_output
assert node_output('retry_planner.plan',json.dumps({'retry_guidance':'revise argument'}))=={
    'retry_guidance':'revise argument'}
print('PASS: Repair returns guidance only; error summaries and raw logs excluded from planning.')
assert goal_prompts.PLANNER.format(direction=goal_prompts.SINGLE_DIRECTION).replace(
    goal_prompts.SINGLE_DIRECTION, '') == goal_prompts.PLANNER.format(
    direction=goal_prompts.DUAL_DIRECTION).replace(goal_prompts.DUAL_DIRECTION, '')
print('PASS: shared lane prompts; one Nature call per round; Repair guidance reaches Lean retry.')
print('PASS: Single sees problem and both goals and chooses; each Dual planner sees problem and its assigned current goal; two fixed lanes, no answer keys or old False Reasoner.')
# Check configured node routing without making model API calls.
from argparse import Namespace
from dotenv import dotenv_values
from single_orchestrator.proofnet_cli import _prompt_models as single_routes
from single_orchestrator.dual_proofnet_cli import _prompt_models as dual_routes
settings=dotenv_values(ROOT/'configs/two_goal_experiment.env')
args=Namespace(orchestrator_model=settings['ORCHESTRATOR_MODEL'],
    nature_model=settings['NATURAL_MODEL'], lean_model=settings['LEAN_MODEL'],
    retry_model=settings['RETRY_MODEL'], false_judgment_model=settings['FALSE_REASON_MODEL'])
assert args.orchestrator_model=='openai/gpt-6-luna-pro'
assert {args.nature_model,args.lean_model,args.retry_model,args.false_judgment_model}=={'openai/gpt-6-luna'}
for mode,client,routes in [('single',single_routes,single_routes(args)),('dual',dual_routes,dual_routes(args))]:
 planners=['orchestrator.strategy','orchestrator.restrategy'] if mode=='single' else ['dual_orchestrator.truth_strategy','dual_orchestrator.falsity_strategy']
 assert all(routes[name]=='openai/gpt-6-luna-pro' for name in planners)
 prefixes=[''] if mode=='single' else ['truth_','falsity_']
 for prefix in prefixes:
  for node in ('nature_agent.initial','nature_agent.retry','lean_generator.generate','retry_planner.plan'):
   assert routes[prefix+node]=='openai/gpt-6-luna'
print('PASS: independent Natural inputs, Lean-only strategy routed to Generator and memory; Luna Pro planners / Luna workers.')
if '--offline-only' in sys.argv:
 print('PASS: offline checks complete; no real Lean builds or model calls.')
 sys.exit(0)
out=ROOT/'.runs/two_goal_smoke_20261014';out.mkdir(exist_ok=True)
project=ROOT/'.datasets/miniF2F-lean4';verifier=LakeVerifier(project,module_name='MiniF2F.Scratch.TwoGoalSmoke',lean_file=project/'MiniF2F/Scratch/TwoGoalSmoke.lean',prelude='import MiniF2F.ProblemImports',timeout_seconds=120)
results=[]
for mode,statement,expected in [('single','theorem arithmetic_false : (2 : ℕ) = 3 := by','false'),('dual','theorem arithmetic_true : (2 : ℕ) = 2 := by','solved')]:
 mem=JsonlTrialMemory(out/f'{mode}.trial_memory.jsonl');mem.clear()
 engine=GoalSearch(model_client=Client(),verifier=verifier,memories={'prove':mem,'disprove':mem},mode=mode,max_trials=1,max_rounds=2 if mode=='single' else 1)
 r=engine.run(problem='Arithmetic example',formal_statement=statement)
 assert r['final_status']==expected,r
 assert all(x['verifier_result'].get('returncode') in (0,1) for x in r['trial_memory'])
 (out/f'{mode}.result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
 results.append({'mode':mode,'status':r['final_status'],'rounds':r['rounds'],'tasks':[x['task'] for x in r['trial_memory']]})
# New protocol sends legal multiline arguments to Lean, avoiding old false positives.
assert GoalVerifier._cheap_malformed_lean_log('have h := id (by\n  intro x\n  rfl)')==''
(out/'summary.json').write_text(json.dumps({'results':results,'model_api_calls':0,'checks':'passed'},indent=2)+'\n')
print(json.dumps(results,indent=2))
