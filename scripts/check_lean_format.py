"""No-network checks for safe proof packaging and canonical retry inputs."""
from pathlib import Path
import json,sys,tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from single_orchestrator.lean_format import normalize_lean_proof
from single_orchestrator.goal_search import GoalSearch
from single_orchestrator.memory import JsonlTrialMemory
raw='by<br>  intro h<BR />  exact h'
assert normalize_lean_proof(raw)[0]=='intro h\nexact h'
protected=['exact "<br>"','exact r##"<br>\\"##','exact «a<br>b»','-- keep <br>\ntrivial','/- outer <br> /- nested <br/> -/ -/\ntrivial','exact "escaped \\" <br>"']
for s in protected:
 assert normalize_lean_proof(s)[0]==s,(s,normalize_lean_proof(s))
for s in [raw,*protected,'norm_num','by\n  intro h\n  exact h','by intro h\n  exact h','by']:
 a,_=normalize_lean_proof(s);b,_=normalize_lean_proof(a);assert a==b
class Client:
 def __init__(self):self.calls=[];self.n=0
 def complete(self,p):
  self.calls.append(p)
  if 'nature_agent' in p.name:return 'The equality follows by reflexivity.'
  if 'lean_generator' in p.name:
   self.n+=1
   if self.n==1:return json.dumps({'lean_proof':'by<br>  exact nonexistent_lemma'})
   assert p.inputs['previous_lean_proof']=='exact nonexistent_lemma'
   assert 'Before:' in p.inputs['repair_guidance']
   return json.dumps({'lean_proof':['rfl']})
  assert p.inputs['lean_proof']=='exact nonexistent_lemma'
  return json.dumps({'retry_guidance':'Before: exact nonexistent_lemma\nAfter: rfl','lean_error_summary':'unknown identifier nonexistent_lemma'})
class Verifier:
 prelude=''
 def verify(self,source,formal_statement):
  assert '<br>' not in source
  ok='  rfl' in source;name=formal_statement.split()[1]
  return {'success':ok,'formal_state':source,'lean_error_log':f"'{name}' does not depend on any axioms" if ok else 'unknown identifier nonexistent_lemma','returncode':0 if ok else 1}
for mode in ('single','dual'):
 with tempfile.TemporaryDirectory() as t:
  c=Client();e=GoalSearch(model_client=c,verifier=Verifier(),memories={k:JsonlTrialMemory(Path(t)/(k+'.jsonl')) for k in ['prove','disprove']},mode=mode,max_trials=3,max_rounds=1)
  r=e.lane('prove',{'prove':'theorem format_retry : (1 : Nat) = 1 := by','disprove':'unused'},'Equality',[],1,lean_strategy='rfl')
  assert r['success'] and len(r['memory'])==2
  first=r['memory'][0];assert first['model_lean_proof']=='by<br>  exact nonexistent_lemma'
  assert first['lean_proof']=='exact nonexistent_lemma' and 'html_line_breaks' in first['proof_format_normalization']
  assert len([p for p in c.calls if 'nature_agent' in p.name])==1
print('PASS: HTML packaging, protected literals/comments, idempotence, raw preservation, canonical Repair/retry inputs and early success; zero API calls.')
# Packaging never renames identifiers or repairs mathematical terms.
unchanged='exact Set.mem_image.mp h\n-- λ stays unchanged\nexact "<br>"'
assert normalize_lean_proof(unchanged)[0]==unchanged
from single_orchestrator.formal_false_review import FormalFalseReviewRuntime
runtime=FormalFalseReviewRuntime(None,Verifier())
blocked=runtime.verify_claim(claim={'lean_proof':'by<br>  axiom bad : False<br>  exact bad'},target='theorem guard_format : False := by')
assert not blocked['verifier_result']['success'] and 'Prohibited construct' in blocked['verifier_result']['lean_error_log']
print('PASS: term preservation and post-normalization proof guard.')
