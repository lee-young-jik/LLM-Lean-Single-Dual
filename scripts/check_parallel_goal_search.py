"""Deterministic concurrency/barrier/cancellation tests; zero model calls."""
from pathlib import Path
from threading import Barrier, Event, Lock
import runpy, tempfile, time
ns=runpy.run_path(str(Path(__file__).with_name('check_forced_goal_search.py')))
Client,Verifier,GoalSearch,Memory=(ns[k] for k in ('Client','Verifier','GoalSearch','JsonlTrialMemory'))
class ParallelClient(Client):
    def __init__(self):super().__init__('Attempt the assigned goal.');self.barrier=Barrier(2);self.rounds={'prove':0,'disprove':0};self.snapshots=[]
    def complete(self,p):
        if 'strategy' in p.name:
            task=p.inputs['assigned_task'];self.rounds[task]+=1
            mem=p.inputs.get('trial_memory',[])
            self.snapshots.append((self.rounds[task],task,mem))
            if self.rounds[task]==2:
                assert len(mem)==2 and {x['task'] for x in mem}=={'prove','disprove'}
                assert all(len(x['trials'])==3 for x in mem)
        if 'nature_agent' in p.name:self.barrier.wait(timeout=5)
        return super().complete(p)
class SerializedVerifier(Verifier):
    def __init__(self):super().__init__();self.active=0;self.guard=Lock()
    def verify(self,*a,**kw):
        with self.guard:self.active+=1;assert self.active==1,'Scratch verifier raced'
        time.sleep(.005)
        try:return super().verify(*a,**kw)
        finally:
            with self.guard:self.active-=1

def run(c,v):
    with tempfile.TemporaryDirectory() as d:
        e=GoalSearch(model_client=c,verifier=v,mode='dual',max_trials=3,max_rounds=2,
            memories={t:Memory(Path(d)/(t+'.jsonl')) for t in ('prove','disprove')})
        return e.run(problem='Public',formal_statement='theorem parallel_fixture : (2 : ℕ) = 3 := by')
c=ParallelClient();v=SerializedVerifier();r=run(c,v)
assert r['rounds']==2 and len(v.calls)==12 and len(c.snapshots)==4
assert c.snapshots[0][2]==c.snapshots[1][2]==[]
assert c.snapshots[2][2]==c.snapshots[3][2]
# Force the losing lane to have an in-flight Natural call when its sibling wins.
started=Event();won=Event()
class EarlyClient(Client):
    def complete(self,p):
        if 'nature_agent' in p.name:
            if p.inputs['task']=='disprove':started.set();assert won.wait(5)
            else:assert started.wait(5)
        return super().complete(p)
class Winner(Verifier):
    def verify(self,*a,**kw):
        r=super().verify(*a,**kw);won.set();return r
c=EarlyClient('Attempt the goal.');v=Winner('prove');r=run(c,v)
assert r['final_status']=='solved' and r['rounds']==1 and v.calls==['prove']
assert not any('falsity_lean_generator' in p.name for p in c.calls)
assert not any('retry_planner' in p.name for p in c.calls)
print('PASS: overlapping lanes, shared prior-round snapshot, barrier before next round, serialized scratch builds, sibling cancellation before Lean, no round after success; zero API calls.')

# A late response fault in the losing lane must not discard a verified sibling.
from single_orchestrator.goal_search import _lane_stop
from single_orchestrator.goal_api import GoalProtocolError
started.clear();won.clear()
class LateFaultClient(EarlyClient):
    def complete(self,p):
        if 'nature_agent' in p.name and p.inputs['task']=='disprove':
            started.set()
            assert _lane_stop.get().wait(5), 'Sibling did not finish verification'
            raise GoalProtocolError('Injected truncated losing-lane response')
        return super().complete(p)
c=LateFaultClient('Attempt the goal.');v=Winner('prove');r=run(c,v)
assert r['final_status']=='solved' and v.calls==['prove']
print('PASS: losing-lane response errors after verified sibling success are cancellation, not a failed evaluation; zero API calls.')
