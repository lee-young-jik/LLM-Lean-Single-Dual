"""Offline paired analysis. Labels are loaded here only, never in proof search."""
from __future__ import annotations
import argparse
from collections import defaultdict, Counter
import csv
import json
import math
from pathlib import Path
import random
import statistics


def ratio(a,b): return a/b if b else None

def mcnemar(b,c):
    n=b+c
    return min(1.,2*sum(math.comb(n,i) for i in range(min(b,c)+1))/2**n) if n else 1.


def bootstrap(values, seed=42, samples=5000):
    if not values: return None
    rng=random.Random(seed)
    means=sorted(statistics.mean(rng.choices(values,k=len(values))) for _ in range(samples))
    return [means[int(.025*(samples-1))],means[int(.975*(samples-1))]]


def read_events(path):
    # Abruptly killed processes may leave one incomplete final line.
    events=[]
    for i,line in enumerate(path.read_text().splitlines()):
        try: events.append(json.loads(line))
        except json.JSONDecodeError: raise ValueError(f'Invalid event line {i+1}: {path}')
    return events


def collect(root,labels):
    rows=[]
    for file in sorted(root.rglob('audit/*/manifest.json')):
        m=json.loads(file.read_text());directory=file.parent
        outcome_path=directory/'outcome.json'
        o=json.loads(outcome_path.read_text()) if outcome_path.exists() else {'execution_status':'incomplete'}
        events=read_events(directory/'events.jsonl');r=o.get('result',{})
        identifier=m['problem_id'];label=labels.get(identifier,{})
        truth=label.get('verified_task')
        predicted={'solved':'prove','false':'disprove','not_solved':'unsolved'}.get(r.get('final_status'),'invalid')
        completed=o['execution_status']=='completed' and not r.get('validation_conflict')
        apis=[e for e in events if e['event']=='api_response']
        usages=[e.get('response',{}).get('usage') or {} for e in apis]
        costs=[u.get('cost') for u in usages]
        builds=[e for e in events if e['event']=='lean_verification']
        plans=[e for e in events if e['event']=='node_started' and 'strategy' in e.get('metadata',{}).get('node','')]
        responses={e['node_id']:e['outputs'] for e in events if e['event']=='node_output'}
        strategies=[{'round':e['metadata']['round'],'task':responses.get(e['node_id'],{}).get('task'),
                     'strategy':responses.get(e['node_id'],{}).get('strategy'),
                     'lean_strategy':responses.get(e['node_id'],{}).get('lean_strategy'),
                     'memory_groups':len(e.get('inputs',{}).get('trial_memory',[]))} for e in plans]
        row={'problem_id':identifier,'mode':m['mode'],'memory_condition':m['memory_condition'],
             'experiment_id':m['experiment_id'],'repetition':m['repetition'],
             'book_key':label.get('book_key','unknown'),
             'execution_status':o['execution_status'],'valid':completed,'truth':truth,
             'prediction':predicted,'correct':predicted==truth if completed and truth else None,
             'rounds':r.get('rounds'),'trials':r.get('trials'),
             'wall_seconds':o.get('wall_seconds'), 'api_requests':sum(e['event']=='api_request' for e in events),
             'api_errors':sum(e['event']=='api_error' for e in events),
             'output_rejections':sum(e['event']=='api_attempt_rejected' and e.get('status_code') is None for e in events),
             'api_response_latency_seconds':sum(e['duration_seconds'] for e in apis) if apis else None,
             'time_to_verified_seconds':min((e['elapsed_seconds'] for e in builds if e['result']['verifier_result'].get('success')),default=None),
             'api_responses':len(apis),'usage_reported_responses':sum(bool(u) for u in usages),
             'prompt_tokens':sum(u.get('prompt_tokens',0) for u in usages) if usages and all('prompt_tokens' in u for u in usages) else None,
             'completion_tokens':sum(u.get('completion_tokens',0) for u in usages) if usages and all('completion_tokens' in u for u in usages) else None,
             'reasoning_tokens':sum((u.get('completion_tokens_details') or {}).get('reasoning_tokens',0) for u in usages) if usages and all((u.get('completion_tokens_details') or {}).get('reasoning_tokens') is not None for u in usages) else None,
             'cost_usd':sum(costs) if costs and all(isinstance(c,(int,float)) for c in costs) else None,
             'lean_builds':len(builds),'lean_successes':sum(bool(e['result']['verifier_result'].get('success')) for e in builds),
             'audit_dir':str(directory),'strategies':strategies,
             'integrity':{'event_sequence_contiguous':[e['sequence'] for e in events]==list(range(1,len(events)+1)),
                 'unclosed_nodes':len({e['node_id'] for e in events if e['event']=='node_started'}-{e['node_id'] for e in events if e['event']=='node_finished'}),
                 'last_event':events[-1]['event'] if events else None}}
        rows.append(row)
    return rows


def metrics(rows):
    valid=[r for r in rows if r['valid'] and r['truth']]
    matrix={t:dict(Counter(r['prediction'] for r in valid if r['truth']==t)) for t in ('prove','disprove')}
    result={'runs':len(rows),'valid_labeled_runs':len(valid),'execution_errors':len(rows)-sum(r['valid'] for r in rows),
            'accuracy':statistics.mean(r['correct'] for r in valid) if valid else None,
            'coverage':ratio(sum(r['prediction'] in ('prove','disprove') for r in valid),len(valid)),
            'confusion_matrix':matrix,'classes':{}}
    for task in ('prove','disprove'):
        tp=sum(r['truth']==task and r['prediction']==task for r in valid)
        fp=sum(r['truth']!=task and r['prediction']==task for r in valid)
        # Abstention is a missed positive for recall, but remains a separate matrix column.
        fn=sum(r['truth']==task and r['prediction']!=task for r in valid)
        result['classes'][task]={'tp':tp,'fp':fp,'fn_including_unsolved':fn,
            'precision':ratio(tp,tp+fp),'recall':ratio(tp,tp+fn),'f1':ratio(2*tp,2*tp+fp+fn)}
    successes=[r for r in valid if r['correct']]
    result['solved_mean_rounds']=statistics.mean(r['rounds'] for r in successes) if successes else None
    for field in ('wall_seconds','lean_builds','api_requests','api_errors','output_rejections','prompt_tokens','completion_tokens','reasoning_tokens','cost_usd','api_response_latency_seconds','time_to_verified_seconds'):
        values=[r[field] for r in rows if r.get(field) is not None]
        result[field]={'sum_reported':sum(values) if values else None,
                       'mean_reported':statistics.mean(values) if values else None,'reported_runs':len(values)}
    result['cost_per_correct_usd']=(sum(r['cost_usd'] for r in rows)/len(successes)
        if successes and all(r['cost_usd'] is not None for r in rows) else None)
    per_repeat=defaultdict(list);per_problem=defaultdict(list)
    for r in valid:
        per_repeat[r['repetition']].append(int(r['correct']))
        per_problem[r['problem_id']].append(int(r['correct']))
    rates=[statistics.mean(x) for x in per_repeat.values()]
    result['repetition_accuracy']={k:statistics.mean(v) for k,v in per_repeat.items()}
    result['repetition_accuracy_std']=statistics.stdev(rates) if len(rates)>1 else None
    result['problem_cluster_bootstrap_accuracy_ci95']=bootstrap([statistics.mean(x) for x in per_problem.values()])
    result['strategy_diagnostics']={'exact_repeated_plan_pairs':0,'comparable_plan_pairs':0,
        'note':'Exact text equality only; semantic strategy quality requires inspection.'}
    for row in rows:
        previous={}
        for plan in sorted(row.get('strategies',[]),key=lambda x:x['round']):
            task=plan['task']
            if task in previous:
                result['strategy_diagnostics']['comparable_plan_pairs']+=1
                if (plan['strategy'],plan['lean_strategy'])==previous[task]:
                    result['strategy_diagnostics']['exact_repeated_plan_pairs']+=1
            previous[task]=(plan['strategy'],plan['lean_strategy'])
    result['pipeline_pass_at_k']={}
    for k in (1,5,10):
        eligible=[v for v in per_problem.values() if len(v)>=k]
        result['pipeline_pass_at_k'][str(k)]={'eligible_problems':len(eligible),
            'value':statistics.mean(1-math.comb(len(v)-sum(v),k)/math.comb(len(v),k) for v in eligible) if eligible else None}
    return result


def analyze(rows):
    grouped=defaultdict(list)
    for r in rows: grouped[(r['experiment_id'],r['memory_condition'],r['mode'])].append(r)
    summary={'groups':{' | '.join(k):metrics(v) for k,v in grouped.items()},'paired':{},
             'notes':['Unsolved is abstention, not a false declaration.',
                      'Execution errors are excluded from accuracy and reported separately.',
                      'Pass@k uses independent complete pipeline runs, not within-round Lean retries.',
                      'Bootstrap resamples problems, keeping repetitions within each problem together.',
                      'Missing usage/cost stays null; reported totals may be incomplete.',
                      'Equal rounds do not imply equal Single/Dual compute budgets.']}
    indexed={}
    for r in rows:
        key=(r['experiment_id'],r['memory_condition'],r['problem_id'],r['repetition'],r['mode'])
        if key in indexed: raise ValueError(f'Duplicate run cell {key}; use separate experiment IDs for reruns')
        indexed[key]=r
    for exp,condition in sorted({(r['experiment_id'],r['memory_condition']) for r in rows}):
        pairs=[]
        for key,s in indexed.items():
            if key[:2]!=(exp,condition) or key[-1]!='single': continue
            d=indexed.get((*key[:-1],'dual'))
            if d and s['correct'] is not None and d['correct'] is not None:pairs.append((s,d))
        by_repeat=defaultdict(list);clusters=defaultdict(list)
        for s,d in pairs:
            by_repeat[s['repetition']].append((s,d));clusters[s['problem_id']].append(int(d['correct'])-int(s['correct']))
        values=[statistics.mean(v) for v in clusters.values()]
        summary['paired'][f'{exp} | {condition}']={'paired_runs':len(pairs), 'paired_problems':len(clusters),
            'dual_minus_single_accuracy':statistics.mean(values) if values else None,
            'problem_cluster_bootstrap_difference_ci95':bootstrap(values),
            'mcnemar_by_repetition':{rep:{'single_only':sum(s['correct'] and not d['correct'] for s,d in ps),
                'dual_only':sum(d['correct'] and not s['correct'] for s,d in ps),
                'exact_two_sided_p':mcnemar(sum(s['correct'] and not d['correct'] for s,d in ps),sum(d['correct'] and not s['correct'] for s,d in ps))} for rep,ps in by_repeat.items()}}
    summary['by_book']={}
    book_groups=defaultdict(list)
    for r in rows:book_groups[(r['experiment_id'],r['memory_condition'],r['mode'],r.get('book_key','unknown'))].append(r)
    summary['by_book']={' | '.join(k):metrics(v) for k,v in book_groups.items()}
    summary['memory_ablation']={}
    for exp,mode in sorted({(r['experiment_id'],r['mode']) for r in rows}):
        pairs=[]
        for key,full in indexed.items():
            if key[0]!=exp or key[1]!='full' or key[-1]!=mode:continue
            none=indexed.get((exp,'none',key[2],key[3],mode))
            if none and full['correct'] is not None and none['correct'] is not None:pairs.append((full,none))
        clusters=defaultdict(list)
        for full,none in pairs:clusters[full['problem_id']].append(int(full['correct'])-int(none['correct']))
        values=[statistics.mean(v) for v in clusters.values()]
        summary['memory_ablation'][f'{exp} | {mode}']={'paired_runs':len(pairs),
            'full_minus_none_accuracy':statistics.mean(values) if values else None,
            'problem_cluster_bootstrap_difference_ci95':bootstrap(values)}
    return summary


def main():
    p=argparse.ArgumentParser(__doc__);p.add_argument('root',type=Path)
    p.add_argument('--labels',type=Path,required=True);p.add_argument('--output-dir',type=Path)
    p.add_argument('--metadata',type=Path,help='Optional public problem JSONL with book_key for subject breakdown')
    a=p.parse_args();out=a.output_dir or a.root/'analysis';out.mkdir(parents=True,exist_ok=True)
    labels={x['id']:x for x in map(json.loads,a.labels.read_text().splitlines())}
    if a.metadata:
        metadata={x.get('id') or x['name']:x for x in map(json.loads,a.metadata.read_text().splitlines())}
        labels={k:{'book_key':metadata.get(k,{}).get('book_key','unknown'),**v} for k,v in labels.items()}
    rows=collect(a.root,labels);summary=analyze(rows)
    jobs_file=a.root/'jobs.jsonl';plan_file=a.root/'plan.json'
    jobs=[json.loads(s) for s in jobs_file.read_text().splitlines()] if jobs_file.exists() else []
    plan=json.loads(plan_file.read_text()) if plan_file.exists() else {}
    summary['execution_inventory']={'planned_jobs':len(plan['jobs']) if 'jobs' in plan else None,
        'finished_processes':len(jobs) if jobs_file.exists() else None,
        'failed_processes':sum(j['returncode']!=0 for j in jobs) if jobs_file.exists() else None,
        'audit_runs':len(rows),'finished_processes_without_audit':max(0,len(jobs)-len(rows)) if jobs_file.exists() else None}
    (out/'runs.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    with (out/'runs.csv').open('w',newline='') as f:
        fields=[k for k in rows[0] if k not in ('strategies','integrity')] if rows else ['problem_id']
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
    (out/'README.md').write_text('# Experiment analysis\n\n```json\n'+json.dumps(summary,ensure_ascii=False,indent=2)+'\n```\n')
    print(f'{len(rows)} runs analyzed: {out}')


if __name__=='__main__':main()
