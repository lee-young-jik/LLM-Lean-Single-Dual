"""Plan/repeat Single-Dual experiments with durable local logs and no answer input."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
from dotenv import dotenv_values, load_dotenv

ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser(__doc__)
    p.add_argument('--jsonl',type=Path,required=True,help='Public evaluator-compatible problem JSONL; never labels.jsonl')
    p.add_argument('--split',required=True);p.add_argument('--ids',nargs='+',required=True)
    p.add_argument('--project-root',type=Path,required=True)
    p.add_argument('--config-env',type=Path,default=ROOT/'configs/two_goal_experiment.env')
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--repetitions',type=int,default=5)
    p.add_argument('--modes',nargs='+',choices=['single','dual'],default=['single','dual'])
    p.add_argument('--memory-conditions',nargs='+',choices=['full','none'],default=['full'])
    p.add_argument('--order-seed',type=int,default=42)
    p.add_argument('--run',action='store_true',help='Execute paid requests; otherwise only save the plan')
    a=p.parse_args()
    if a.repetitions<1: p.error('repetitions must be positive')
    if len(set(a.ids))!=len(a.ids) or len(set(a.memory_conditions))!=len(a.memory_conditions):p.error('Duplicate IDs or conditions')
    public=a.jsonl.resolve();records=[json.loads(line) for line in public.read_text().splitlines()]
    forbidden={'truth_label','verified_task','lean_proof','certificate','expected_false_fix','false_transform'}
    if any(forbidden.intersection(r) for r in records):p.error('Input includes private answer fields; use a public-only export')
    if any(identifier not in {r.get('id') or r.get('name') for r in records} for identifier in a.ids):p.error('Unknown problem ID')
    sys.path.insert(0,str(ROOT/'src'))
    from single_orchestrator.proofnet import load_proofnet_jsonl
    examples={x.id:x for x in load_proofnet_jsonl(public,split=a.split)}
    if not set(a.ids)<=examples.keys():p.error('Selected IDs are not present in the specified split')
    settings=dict(dotenv_values(a.config_env));load_dotenv(ROOT/'.env')
    out=a.output_dir.resolve();out.mkdir(parents=True,exist_ok=False)
    env=os.environ.copy();env.update(PYTHONPATH=str(ROOT/'src'),DUAL_ARC_SEARCH_ARCHITECTURE='two_goal',
        DUAL_ARC_FALSE_DECISION_MODE='two_goal',DUAL_ARC_MODEL_CLIENT='cli',
        DUAL_ARC_API_MAX_ATTEMPTS=str(settings.get('DUAL_ARC_API_MAX_ATTEMPTS',settings.get('API_MAX_ATTEMPTS',3))),
        DUAL_ARC_OPENROUTER_PROVIDER=str(settings.get('DUAL_ARC_OPENROUTER_PROVIDER',settings.get('OPENROUTER_PROVIDER',''))),
        DUAL_ARC_OPENROUTER_MODEL_PROVIDERS=str(settings.get('DUAL_ARC_OPENROUTER_MODEL_PROVIDERS',settings.get('OPENROUTER_MODEL_PROVIDERS','{}'))))
    for key in ('DUAL_ARC_API_MAX_TRANSPORT_ATTEMPTS','DUAL_ARC_MAX_OUTPUT_TOKENS',
                'DUAL_ARC_REASONING_EFFORT','DUAL_ARC_MODEL_REASONING_EFFORTS',
                'DUAL_ARC_LEAN_DECLARATIONS','DUAL_ARC_REPAIR_CONTEXT',
                'DUAL_ARC_REPAIR_MAX_OUTPUT_TOKENS','DUAL_ARC_REPAIR_TIMEOUT_SECONDS'):
        if settings.get(key) is not None: env[key]=str(settings[key])
    env['DUAL_ARC_EXPERIMENT_ID']=out.name
    jobs=[(rep,condition,identifier,mode) for rep in range(1,a.repetitions+1)
          for condition in a.memory_conditions for identifier in a.ids for mode in a.modes]
    random.Random(a.order_seed).shuffle(jobs)
    sys.path.insert(0,str(ROOT/'src'))
    from single_orchestrator.local_audit import safe
    plan={'created_utc':datetime.now(timezone.utc).isoformat(),'experiment_id':out.name,
          'public_data_sha256':hashlib.sha256(public.read_bytes()).hexdigest(),
          'settings':settings,'arguments':vars(a),'jobs':jobs,
          'note':'Order seed randomizes scheduling only. Each repetition is a fresh complete pipeline; no answer file is passed.'}
    (out/'plan.json').write_text(json.dumps(safe(plan),default=str,ensure_ascii=False,indent=2)+'\n')
    if not a.run: print(f'Plan only: {len(jobs)} executions; no model calls. {out}');return
    for rep,condition,identifier,mode in jobs:
        folder=out/f'repeat_{rep:02}'/condition/identifier/mode;folder.mkdir(parents=True)
        env.update(DUAL_ARC_MEMORY_CONDITION=condition,DUAL_ARC_REPETITION=str(rep))
        module='MiniF2F.Scratch.Audit'+hashlib.sha256(str(folder).encode()).hexdigest()[:20]
        cmd=[sys.executable,'-m','single_orchestrator.'+('proofnet_cli' if mode=='single' else 'dual_proofnet_cli'),
            '--jsonl',str(public),'--split',a.split,'--ids',identifier,'--limit','1',
            '--project-root',str(a.project_root.resolve()),'--output-dir',str(folder),'--output',str(folder/'results.jsonl'),
            '--model',str(settings['NATURAL_MODEL']),'--nature-model',str(settings['NATURAL_MODEL']),
            '--orchestrator-model',str(settings['ORCHESTRATOR_MODEL']),'--lean-model',str(settings['LEAN_MODEL']),
            '--retry-model',str(settings['RETRY_MODEL']),'--strict-model-routing',
            '--max-trials',str(settings.get('MAX_TRIALS',3)),
            '--max-strategy-rounds' if mode=='single' else '--max-rounds',str(settings.get('MAX_ROUNDS',3)),
            '--model-timeout',str(settings.get('MODEL_TIMEOUT_SETS_01_03',300)),
            '--verify-timeout',str(settings.get('VERIFY_TIMEOUT',60)),
            '--verifier-module',module,'--verifier-file',str(a.project_root.resolve()/(module.replace('.','/')+'.lean')),
            '--verifier-prelude',examples[identifier].header or 'import Mathlib']
        print(f'Start repeat={rep} memory={condition} id={identifier} mode={mode}',flush=True)
        with (folder/'console.log').open('w') as console:
            rc=subprocess.run(cmd,cwd=ROOT,env=env,stdout=console,stderr=subprocess.STDOUT).returncode
        with (out/'jobs.jsonl').open('a') as f:
            f.write(json.dumps({'repetition':rep,'memory_condition':condition,'problem_id':identifier,
                               'mode':mode,'returncode':rc,'output_dir':str(folder)})+'\n');f.flush();os.fsync(f.fileno())
    print(f'Finished. Analyze offline with scripts/analyze_logged_experiment.py: {out}')


if __name__=='__main__':main()
