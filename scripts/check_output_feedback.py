"""No-network checks for rejection-aware generation and the production adapter."""
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from single_orchestrator.goal_api import complete_chat_with_retries,GoalProtocolError
from single_orchestrator.prompts import BuiltPrompt
from single_orchestrator.model import OpenAICompatibleModelClient

def response(raw):return NS(choices=[NS(finish_reason='stop',message=NS(content=raw))])
p=BuiltPrompt(name='falsity_lean_generator.generate',version='v18-format-repair-lines',inputs={'task':'disprove','goal':'theorem fixed_goal : True := by'},text='Original fixed goal and natural argument')
bad=json.dumps({'lean_proof':['theorem changed : True := by','  trivial']});good=json.dumps({'lean_proof':['trivial']})
requests=[];events=[]
def create(**kw):
 messages=kw['messages'];requests.append(messages)
 if len(messages)==1:return response(bad)
 assert messages[0]['content']==p.text and messages[1]['content']==bad
 assert 'Forbidden proof packaging or construct: theorem' in messages[-1]['content']
 assert 'Remove theorem/lemma declarations' in messages[-1]['content']
 assert 'do not change the supplied goal' in messages[-1]['content']
 return response(good)
raw,_=complete_chat_with_retries(p,create,on_attempt=lambda n,r,e:events.append((n,str(e) if e else None)),wait=lambda _:None)
assert raw==good and len(requests)==2 and len(events)==2
assert events[0][1] and events[1][1] is None
assert p.text=='Original fixed goal and natural argument' and p.inputs['goal']=='theorem fixed_goal : True := by'
# Transport retry keeps the precise corrective request, rather than adding new feedback.
class RateLimit(Exception):status_code=429;response=NS(headers={})
seen=[];sequence=iter([response(bad),RateLimit('busy'),response(good)]);waits=[]
def transient(**kw):
 seen.append(kw['messages']);value=next(sequence)
 if isinstance(value,Exception):raise value
 return value
complete_chat_with_retries(p,transient,wait=waits.append)
assert len(seen)==3 and seen[1]==seen[2] and waits==[.5,5]
# Repeated invalid output still stops after three requests; only latest bad output retained.
seen=[]
def invalid(**kw):seen.append(kw['messages']);return response(bad)
try:complete_chat_with_retries(p,invalid,wait=lambda _:None)
except GoalProtocolError:pass
else:raise AssertionError('Terminal malformed output was accepted')
assert [len(m) for m in seen]==[1,3,3]
# Natural abandonment error gets the exact lane-specific required opening.
n=BuiltPrompt(name='falsity_nature_agent.initial',version=p.version,inputs={'task':'disprove'},text='Attempt selected disprove goal')
seen=[]
def natural(**kw):
 seen.append(kw['messages'])
 if len(seen)==1:return response('ABANDON: This cannot hold. The original statement is true.')
 assert 'ABANDON: The original statement is true.' in kw['messages'][-1]['content']
 return response('ABANDON: The original statement is true. A direct mathematical argument establishes it.')
complete_chat_with_retries(n,natural,wait=lambda _:None)
# Production model client really forwards feedback to the SDK; no real API call.
requests.clear()
fake=NS(base_url='https://openrouter.ai/api/v1',chat=NS(completions=NS(create=create)))
with patch.dict('os.environ',{'TEST_FEEDBACK_KEY':'test-only'}),patch('openai.OpenAI',return_value=fake):
 client=OpenAICompatibleModelClient(model='test/model',timeout_seconds=0,api_key_env='TEST_FEEDBACK_KEY')
 assert client.complete(p)==good
assert len(requests)==2
# Optional request logger receives exact copies, including correction turns.
logs=[];requests.clear()
complete_chat_with_retries(p,create,on_request=lambda n,m:logs.append((n,m)),wait=lambda _:None)
assert [m for _,m in logs]==requests
print('PASS: rejection + prior output reach the next actual request, fixed task preserved, bounded retries, transport separation, Natural feedback, production SDK routing and exact request logging; zero network/model calls.')
