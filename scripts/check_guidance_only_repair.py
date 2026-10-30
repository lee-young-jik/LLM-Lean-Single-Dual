"""Verify single-output Repair contracts and error-free planner memory without calls."""
from pathlib import Path
from types import SimpleNamespace as NS
import json,os,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from single_orchestrator.prompts import BuiltPrompt
from single_orchestrator.goal_api import response_format,validate_response,GoalOutputError,output_correction,complete_with_retries
from single_orchestrator.planner_memory import planner_memory
from single_orchestrator.model import _output_token_limit
from single_orchestrator.goal_trace import node_output
VERSION='v21-guidance-only-repair'
p=BuiltPrompt(name='retry_planner.plan',version=VERSION,inputs={},text='')
def response(obj,finish='stop'):return NS(choices=[NS(finish_reason=finish,message=NS(content=json.dumps(obj)))])
props=response_format(p)['json_schema']['schema']['properties']
assert set(props)=={'retry_guidance'} and props['retry_guidance']['maxLength']==4000
assert validate_response(p,response({'retry_guidance':'Before: bad; After: fixed'}))
for obj in [{'retry_guidance':'fix','lean_error_summary':'wrong'},{'lean_error_summary':'wrong'},{'retry_guidance':''},{'retry_guidance':'x'*4001}]:
 try:validate_response(p,response(obj))
 except GoalOutputError:pass
 else:raise AssertionError(obj)
assert 'lean_error_summary' not in output_correction(p,'bad')
old={'strategy_round':1,'task':'prove','lean_strategy':'plan','informal':'math','trial_index':1,'lean_code':'CODE','lean_error_summary':'SECRET_SUMMARY','lean_error_log':'SECRET_LOG','retry_plan':'SECRET_GUIDANCE','verifier_result':{'formal_state':'CODE','lean_error_log':'SECRET_LOG'}}
mem=planner_memory([old]);encoded=json.dumps(mem)
assert all(x not in encoded for x in ['SECRET_','lean_error_summary','lean_error_log','retry_plan','retry_guidance'])
assert 'CODE' in encoded and planner_memory(mem)==mem
# Strip guidance while preserving code order and lane isolation, including imported grouped history.
sequence=[{**old,'trial_index':1,'lean_code':'CODE_ONE','retry_plan':'EDIT_ONE'},
          {**old,'trial_index':2,'lean_code':'CODE_TWO','retry_plan':''},
          {**old,'trial_index':3,'lean_code':'CODE_THREE','retry_plan':None},
          {**old,'task':'disprove','trial_index':1,'lean_code':'OTHER_LANE','retry_plan':'OTHER_EDIT'}]
grouped=planner_memory(sequence)
assert grouped[0]['trials']==[{'trial':1,'lean_code':'CODE_ONE'},
 {'trial':2,'lean_code':'CODE_TWO'},{'trial':3,'lean_code':'CODE_THREE'}]
assert grouped[1]['trials']==[{'trial':1,'lean_code':'OTHER_LANE'}]
grouped[0]['trials'][0]['retry_guidance']='IMPORTED_GUIDANCE'
clean=planner_memory(grouped)
assert 'IMPORTED_GUIDANCE' not in json.dumps(clean)
grouped[0]['trials'][0].pop('retry_guidance')
assert clean==grouped and planner_memory(grouped)==grouped
assert sequence[0]['retry_plan']=='EDIT_ONE' and 'retry_guidance' not in sequence[0]
assert node_output(p.name,json.dumps({'retry_guidance':'fix'}),version=VERSION)=={'retry_guidance':'fix'}
assert _output_token_limit(p)==2048
# New contract retains bounded regeneration after a truncated output.
seq=iter([response({},'length'),response({'retry_guidance':'fix'})]);calls=[]
raw,_=complete_with_retries(p,lambda:next(seq),max_attempts=2,on_attempt=lambda *x:calls.append(x),wait=lambda _:None)
assert json.loads(raw)=={'retry_guidance':'fix'} and len(calls)==2
# Planner/Generator contracts keep the existing field and line-array restrictions.
for name,fields in [('orchestrator.strategy',{'task','lean_strategy'}),('lean_generator.generate',{'lean_proof'})]:
 q=BuiltPrompt(name=name,version=VERSION,inputs={},text='')
 assert set(response_format(q)['json_schema']['schema']['properties'])==fields
legacy=BuiltPrompt(name='retry_planner.plan',version='v17-historical',inputs={},text='')
assert validate_response(legacy,response({'retry_guidance':'fix','lean_error_summary':'historical summary'}))
print('PASS: guidance-only strict schema/recovery/length guard, historical error summaries stripped from planner memory, output caps and existing planner/Lean contracts; zero model calls.')
