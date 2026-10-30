"""Single chooses P/¬P each round; Dual always searches both fixed targets."""
from __future__ import annotations
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from threading import Event, Lock
from pathlib import Path
import json
import os
import re
import textwrap
from typing import Any
from .formal_false_review import FormalFalseReviewRuntime, refutation_target
from .false_review import FalseReviewRuntime
from .model import ModelClientError
from .prompts import BuiltPrompt
from . import goal_prompts
from .planner_memory import planner_memory
from .goal_trace import node_trace, node_name, node_output, finish
from .verifier import LakeVerifier
from .lean_format import normalize_lean_proof
from .goal_api import GoalProtocolError, GoalEnvironmentError, validate_natural_output

_lane_stop = ContextVar('goal_lane_stop', default=None)

class _LaneCancelled(RuntimeError):
    """A verified sibling goal ended this search, not a failed proof."""


def _check_lane_stop():
    event = _lane_stop.get()
    if event is not None and event.is_set():
        raise _LaneCancelled('Sibling fixed goal was Lean-verified')


class GoalVerifier(LakeVerifier):
    @staticmethod
    def _cheap_malformed_lean_log(lean_code: str) -> str:
        # Legal multi-line `(by ...)` arguments were falsely rejected here.
        # Lean itself checks syntax. Placeholder/resource guards remain active.
        return ''


def fixed_goals(statement: str) -> dict[str, str]:
    prove = statement.replace('\\n', '\n').strip()
    return {'prove': prove, 'disprove': refutation_target(prove)}


class GoalSearch:
    def __init__(self, *, model_client, verifier, memories, mode,
                 max_trials=3, max_rounds=3):
        self.model_client=model_client;self.mode=mode;self.memories=memories
        self.max_trials=max_trials;self.max_rounds=max_rounds
        if isinstance(verifier, LakeVerifier):
            verifier=GoalVerifier(project_root=verifier.project_root,
                module_name=verifier.module_name,lean_file=verifier.lean_file,
                prelude=verifier.prelude,timeout_seconds=verifier.timeout_seconds,
                strict_timeout_guard=verifier.strict_timeout_guard,
                command_runner=verifier.command_runner)
        self.verifier=verifier
        self.kernel=FormalFalseReviewRuntime(model_client,verifier)
        # Shared scratch files and Lake build outputs must never race.
        self._verification_lock=Lock()

    def complete(self, name, instructions, inputs, *, round_index=None, trial=None, task=None):
        _check_lane_stop()
        if (os.getenv('DUAL_ARC_LEAN_DECLARATIONS', '0') == '1'
                and 'nature_agent' not in name):
            from .lean_declarations import declarations_for_goal
            declarations = declarations_for_goal(self.verifier.project_root,
                self.verifier.prelude, inputs.get('goal') or inputs.get('goals', {}))
            if declarations:
                inputs = {**inputs, 'lean_environment_declarations': declarations}
        inputs={k:v for k,v in inputs.items() if v is not None and v != '' and v != [] and v != {}}
        prompt=BuiltPrompt(name=name,version='v21-guidance-only-repair',inputs=deepcopy(inputs),
            text='System:\n'+instructions+'\nDo not use tools, hidden labels or stored answers.\n\nUser:\n'+json.dumps(inputs,ensure_ascii=False,indent=2))
        label=f'R{round_index}' + (f' · T{trial}' if trial else '') + (f' · {task.upper()}' if task else '')
        with node_trace(f'{label} · {node_name(name)}', inputs,
                metadata={'prompt_version':prompt.version,'node':name,'instructions':instructions,
                          'round':round_index,'trial':trial,'task':task,'mode':self.mode}, run_type='llm') as run:
            try:
                raw=self.model_client.complete(prompt)
                from .local_audit import emit
                emit('model_raw_output', prompt_name=name, raw_response=raw)
                _check_lane_stop()
                validate_natural_output(prompt,raw)
            except Exception as exc:
                # A verified sibling wins even if this in-flight response is
                # truncated or malformed when it finally returns.
                _check_lane_stop()
                if isinstance(exc, (ModelClientError,ValueError,TypeError)):
                    raise GoalProtocolError(f'Node call/contract failed for {name}: {exc}') from exc
                raise
            output=node_output(name,raw,version=prompt.version)
            finish(run,output)
            return raw

    @staticmethod
    def abandonment_reason(output, task=None):
        """Accept explicit stop signals or an opening opposite-value declaration."""
        if isinstance(output, dict):
            if output.get('abandon') is not True:
                return None
            reason = output.get('reason')
        elif isinstance(output, str) and output.strip().startswith('ABANDON:'):
            reason = output.strip()[len('ABANDON:'):]
        elif isinstance(output, str) and task in ('prove', 'disprove'):
            # Compatibility with categorical declarations in prior model outputs.
            # Do not scan arbitrary argument text, quoted text, or compiler logs.
            opening = output.strip().split('\n', 1)[0]
            match = re.match(
                r'\A(?:The|This) (?:(?:original|proposed) )?'
                r'(?:statement|claim|proposition|theorem) is (?:actually )?'
                r'(?P<verdict>not true|not false|false|true)\b(?P<tail>.*)',
                opening, re.IGNORECASE)
            if not match:
                return None
            tail = match.group('tail').split('.', 1)[0]
            if re.search(r'\b(?:if|when|unless|provided|assuming)\b', tail, re.IGNORECASE):
                return None
            verdict = match.group('verdict').lower()
            if ((task == 'prove' and verdict in ('false', 'not true')) or
                    (task == 'disprove' and verdict in ('true', 'not false'))):
                reason = output.strip()
            else:
                return None
        else:
            return None
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('Abandonment requires a nonempty mathematical reason')
        reason=reason.strip()
        if task in ('prove','disprove'):
            declaration=re.match(r'^(?:The|This) (?:(?:original|proposed) )?(?:statement|claim|proposition|theorem) is (?:actually )?(not true|not false|false|true)\b',reason,re.IGNORECASE)
            expected=('false','not true') if task=='prove' else ('true','not false')
            if not declaration or declaration.group(1).lower() not in expected:
                return None
            if re.search(r'beyond (?:the )?scope|within this response|available lemmas|given resources|time limit|token limit|because (?:I|we) (?:cannot|can.t) (?:prove|formalize)',reason,re.IGNORECASE):
                return None
        return reason

    def plan(self, problem, goals, history, round_index, forced_task=None, abandoned_lanes=None):
        inputs={'trial_memory':planner_memory(history, goals) if getattr(self, 'memory_condition', 'full') == 'full' else []}
        if abandoned_lanes:
            inputs['abandoned_lanes']=deepcopy(abandoned_lanes)
        if forced_task:
            inputs['problem']=problem
            inputs['goal']=goals[forced_task]
            inputs['assigned_task']=forced_task
            name='dual_orchestrator.truth_strategy' if forced_task=='prove' else 'dual_orchestrator.falsity_strategy'
            direction=goal_prompts.DUAL_DIRECTION
        else:
            inputs.update(problem=problem,goals=goals)
            name='orchestrator.strategy' if round_index==1 else 'orchestrator.restrategy'
            direction=goal_prompts.SINGLE_DIRECTION
        use_memory=getattr(self, 'memory_condition', 'full') == 'full'
        if not use_memory and forced_task:
            direction=direction.replace("Use both lanes' previous records to revise your assigned approach. ", "")
        instruction=goal_prompts.planner_instruction(direction, use_memory=use_memory)
        if forced_task:
            instruction+='\n'+(goal_prompts.DUAL_PLAN_RESTART if use_memory else
                "For this round, plan a concrete attempt to prove the same assigned goal. LLM truth judgments never disable this lane.")
        try:
            parsed=FalseReviewRuntime._parse_json(self.complete(name,instruction,inputs,round_index=round_index,task=forced_task))
            task=forced_task or parsed.get('task')
            if task not in goals or (forced_task and parsed.get('task') not in (None,forced_task)):
                raise ValueError('Invalid task')
            lean_strategy=parsed.get('lean_strategy')
            if set(parsed) != {'task', 'lean_strategy'}:
                raise ValueError('Planner returns only task and lean_strategy')
            if not isinstance(lean_strategy,str) or not lean_strategy.strip():
                raise ValueError('Planner requires nonempty lean_strategy')
            return {'task':task,'lean_strategy':lean_strategy.strip()}
        except (ModelClientError,ValueError,TypeError) as exc:
            raise GoalProtocolError(f'Planner contract failed; no default strategy substituted: {exc}') from exc

    def lane(self, task, goals, problem, history, round_index, *, lean_strategy, _records=None, _failed_attempts=None):
        records=[] if _records is None else _records;prefix='' if self.mode=='single' else ('truth_' if task=='prove' else 'falsity_')
        informal='';attempt_errors=[] if _failed_attempts is None else _failed_attempts
        for trial in range(1,self.max_trials+1):
            try:
                rejected_abandonments=[]
                model_lean_proof=None;format_changes=[]
                inputs={'task':task,'goal':goals[task],'problem':problem}
                if self.mode == 'dual':
                    inputs['role_assumption'] = goal_prompts.DUAL_ROLES[task]
                try:
                    if not informal:
                        informal=self.complete(prefix+'nature_agent.initial',
                            goal_prompts.NATURAL+('\n'+goal_prompts.DUAL_NATURAL_CONTINUE if self.mode=='dual' else ''),
                            inputs,round_index=round_index,trial=trial,task=task)
                    lean_inputs={'task':task,'goal':goals[task],'informal':informal,'lean_strategy':lean_strategy,'verifier_prelude':self.verifier.prelude}
                    if records:
                        previous=records[-1]
                        lean_inputs.update(previous_lean_proof=previous['lean_proof'],
                            lean_error_log=previous['lean_error_log'],repair_guidance=previous['retry_plan'])
                        if previous.get('repair_context'):
                            lean_inputs['repair_context']=previous['repair_context']
                    if self.mode == 'dual':
                        lean_inputs['role_assumption']=goal_prompts.DUAL_ROLES[task]
                    raw=self.complete(prefix+'lean_generator.generate',
                        goal_prompts.LEAN+('\n'+goal_prompts.DUAL_LEAN_CONTINUE if self.mode=='dual' else ''),
                        lean_inputs,round_index=round_index,trial=trial,task=task)
                    parsed=FalseReviewRuntime._parse_json(raw)
                    if self.mode=='dual' and parsed.get('abandon') is True:
                        rejected_abandonments.append({'stage':'lean_generator','reason':parsed.get('reason','')})
                        raise ValueError('Abandonment is disabled; continue Lean attempts.')
                    body=parsed.get('lean_proof','')
                    model_lean_proof=body
                    if isinstance(body,list) and all(isinstance(line,str) for line in body):
                        body='\n'.join(body)
                        format_changes.append('proof_lines_joined')
                    if not isinstance(body,str):raise TypeError('lean_proof must contain tactic lines')
                    body,packaging_changes=normalize_lean_proof(body)
                    format_changes.extend(packaging_changes)
                    if not body.strip():raise ValueError('No tactic body after normalization')
                    claim={'lean_proof':body}
                except (ModelClientError,ValueError,TypeError) as exc:
                    raise GoalProtocolError(f'Lane node contract failed for {task}; no empty proof substituted: {exc}') from exc
                _check_lane_stop()
                from datetime import datetime, timezone
                verification_started=datetime.now(timezone.utc).isoformat()
                with node_trace(f'R{round_index} · T{trial} · {task.upper()} · {node_name("verifier")}',
                        {'goal':goals[task],'lean_proof':claim['lean_proof']},run_type='tool') as trace:
                    with self._verification_lock:
                        _check_lane_stop()
                        try:
                            result=self.kernel.verify_claim(claim=claim,target=goals[task])
                            from .local_audit import emit
                            emit('lean_verification', task=task, round=round_index, trial=trial, target=goals[task], claim=claim, result=result)
                            vr=result['verifier_result']
                            log=vr.get('lean_error_log','')
                            rc=vr.get('returncode')
                            if vr.get('timed_out') or (isinstance(rc,int) and rc<0) or any(t in log.lower() for t in ('no such file or directory','unknown module prefix','lean verifier execution failed','object file of module')):
                                raise GoalEnvironmentError('Lean environment failed; proof outcome is not scorable: '+log)
                            if rc is None:
                                raise GoalProtocolError('Lean proof rejected before compilation; no proof attempt may be scored: '+log)
                        except GoalProtocolError:
                            # Invalid output/pre-validation is local to this lane.
                            raise
                        except BaseException:
                            if _lane_stop.get() is not None:_lane_stop.get().set()
                            raise
                        if vr.get('success') and _lane_stop.get() is not None:
                            _lane_stop.get().set()
                    finish(trace,{'verified':bool(vr.get('success')),'diagnostics':vr.get('lean_error_log','')})
                verification_finished=datetime.now(timezone.utc).isoformat()
                verified=bool(result['verifier_result'].get('success'))
                retry=''
                repair_context=None
                repair_error=None
                # Repair has one output: concrete guidance for retrying the Lean implementation.
                if not verified:
                    repair_inputs={'task':task,'goal':goals[task],'informal':informal,
                        'lean_strategy':lean_strategy,
                        'lean_proof':claim['lean_proof'],
                        'lean_error_log':result['verifier_result'].get('lean_error_log','')}
                    if os.getenv('DUAL_ARC_REPAIR_CONTEXT', '0') == '1':
                        from .repair_context import build_repair_context
                        repair_context=build_repair_context(lean_proof=claim['lean_proof'],
                            formal_state=result['verifier_result'].get('formal_state', ''),
                            error_log=repair_inputs['lean_error_log'],
                            previous=records[-1] if records else None,
                            source_file=getattr(self.verifier, 'lean_file', None))
                        repair_inputs['repair_context']=repair_context
                    try:
                        repair_raw=self.complete(prefix+'retry_planner.plan',
                            goal_prompts.REPAIR+('\n'+goal_prompts.DUAL_REPAIR_CONTINUE if self.mode=='dual' else ''),
                            repair_inputs,
                            round_index=round_index,trial=trial,task=task)
                        repair=FalseReviewRuntime._parse_json(repair_raw)
                        retry=repair.get('retry_guidance')
                        if set(repair) != {'retry_guidance'} or not isinstance(retry,str):
                            raise ValueError('Repair returns only retry_guidance as a string')
                        if self.mode=='dual' and (repair.get('abandon') is True or retry.strip().startswith('ABANDON:')):
                            raise ValueError('Abandonment is disabled; no default Repair guidance substituted.')
                        if not retry.strip():
                            raise ValueError('Repair requires nonempty retry_guidance')
                    except _LaneCancelled:
                        pass  # Save the completed build; no further repair or trial.
                    except GoalProtocolError as exc:
                        repair_error=exc  # Save the completed build before ending this lane/round.
                    except (ModelClientError,ValueError,TypeError) as exc:
                        repair_error=GoalProtocolError(f'Repair contract failed; no default guidance substituted: {exc}')
                        repair_error.__cause__=exc
                record={'task':task,'lane':'truth' if task=='prove' else 'falsity',
                    'strategy_round':round_index,'trial_index':trial,'lean_strategy':lean_strategy,'informal':informal,
                    'verification_started':verification_started,'verification_finished':verification_finished,
                    'lean_proof':claim['lean_proof'],'lean_code':result['verifier_result'].get('formal_state') or goals[task]+'\n'+textwrap.indent(claim['lean_proof'],'  '),
                    'target':goals[task],'formal_state':result['verifier_result'].get('formal_state',''),
                    'verifier_result':result['verifier_result'],'judge_result':{},
                    'lean_error_log':result['verifier_result'].get('lean_error_log',''),
                    'retry_plan':retry if repair_error is None else ''}
                if repair_context is not None:record['repair_context']=repair_context
                if model_lean_proof is not None and model_lean_proof!=claim['lean_proof']:
                    record['model_lean_proof']=model_lean_proof
                    record['proof_format_normalization']=format_changes
                if rejected_abandonments:record['rejected_abandonments']=rejected_abandonments
                records.append(record);self.memories[task].append(record)
                from .local_audit import emit
                emit('trial_saved', record=record)
                if repair_error is not None:
                    raise repair_error
                if verified:return {'task':task,'success':True,'memory':records,'verification':result,'failed_attempts':attempt_errors}
                _check_lane_stop()
            except GoalProtocolError as exc:
                # A bad node output/pre-check consumes this attempt only.
                # Keep valid earlier code/argument; never fabricate a proof or guidance.
                failure={'task':task,'round':round_index,'trial':trial,
                         'error_type':type(exc).__name__,'error':str(exc)}
                attempt_errors.append(failure)
                from .local_audit import emit
                emit('trial_protocol_error', **failure)
                _check_lane_stop()
        return {'task':task,'success':False,'memory':records,'failed_attempts':attempt_errors}

    def run(self, *, problem, formal_statement, **ignored):
        from .local_audit import AuditSession, SESSION, environment
        self.memory_condition = os.getenv('DUAL_ARC_MEMORY_CONDITION', 'full')
        if self.memory_condition not in ('full', 'none'):
            raise ValueError('DUAL_ARC_MEMORY_CONDITION must be full or none')
        parent = next(iter(self.memories.values())).path.parent
        session = AuditSession(parent, {
            'mode': self.mode, 'memory_condition': self.memory_condition,
            'problem_id': next(iter(self.memories.values())).path.name.split('.truth_memory')[0].split('.falsity_memory')[0].split('.trial_memory')[0],
            'experiment_id': os.getenv('DUAL_ARC_EXPERIMENT_ID', 'unassigned'),
            'repetition': os.getenv('DUAL_ARC_REPETITION', '1'),
            'problem': problem, 'goals': fixed_goals(formal_statement),
            'max_rounds': self.max_rounds, 'max_trials': self.max_trials,
            'memory_paths': {k: str(v.path) for k,v in self.memories.items()},
            **environment(self.verifier, self.model_client)})
        token = SESSION.set(session)
        try:
            result = self._traced_run(problem=problem, formal_statement=formal_statement)
            result['local_audit_dir'] = str(session.path)
            session.finish('completed', result=result)
            return result
        except BaseException as exc:
            session.finish('cancelled' if isinstance(exc, _LaneCancelled) else 'execution_error',
                           error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            SESSION.reset(token)

    def _traced_run(self, *, problem, formal_statement):
        with node_trace(getattr(self,'trace_name',f'{self.mode.title()} | Prove or Disprove'),
                {'problem':problem,'goals':fixed_goals(formal_statement)},
                metadata={'max_rounds':self.max_rounds,'max_trials':self.max_trials}) as trace:
            result=self._run(problem=problem,formal_statement=formal_statement)
            finish(trace,{'result':{'solved':'Proved','false':'Disproved','not_solved':'Unsolved'}[result['final_status']],
                          'rounds':result['rounds'],'attempts':result['trials'],
                          'abandoned_lanes':result['abandoned_lanes'],
                          'validation_conflict':result['validation_conflict'],
                          'lane_protocol_errors':result['lane_protocol_errors'],
                          'failed_attempts':result['failed_attempts']})
            return result

    @staticmethod
    def _protocol_failed_lane(error, task, round_index, records):
        """Keep invalid output visible, without inventing a plan/proof or cancelling a sibling."""
        from .local_audit import emit
        detail={'round':round_index,'task':task,'error_type':type(error).__name__,'error':str(error)}
        emit('lane_protocol_error', **detail)
        return {'task':task,'success':False,'memory':records,'protocol_error':detail}

    def _run(self, *, problem, formal_statement):
        goals=fixed_goals(formal_statement);history=[];rounds=[];abandoned_lanes=[];status='not_solved';winning=None;conflict=False
        lane_protocol_errors=[];failed_attempts=[]
        for round_index in range(1,self.max_rounds+1):
            from .local_audit import emit
            emit('round_started', round=round_index, previous_trials=len(history))
            if self.mode=='single':
                plan=None;records=[]
                try:
                    plan=self.plan(problem,goals,history,round_index)
                    outcomes=[self.lane(plan['task'],goals,problem,
                        deepcopy(history),round_index,lean_strategy=plan['lean_strategy'],_records=records)]
                except GoalProtocolError as exc:
                    outcomes=[self._protocol_failed_lane(exc,plan['task'] if plan else None,round_index,records)]
                plans=[plan] if plan is not None else []
            else:
                # Snapshot barrier: no lane sees this round's sibling records.
                snapshot=deepcopy(history);stop=Event()
                def search_lane(task):
                    records=[];failures=[];plan=None;token=_lane_stop.set(stop)
                    try:
                        plan=self.plan(problem,goals,deepcopy(snapshot),round_index,task)
                        outcome=self.lane(task,goals,problem,
                            deepcopy(snapshot),round_index,lean_strategy=plan['lean_strategy'],_records=records,_failed_attempts=failures)
                        return plan,outcome
                    except _LaneCancelled:
                        return plan,{'task':task,'success':False,'cancelled':True,
                            'cancel_reason':'Sibling fixed goal was Lean-verified','memory':records,'failed_attempts':failures}
                    except GoalProtocolError as exc:
                        return plan,self._protocol_failed_lane(exc,task,round_index,records)
                    except BaseException:
                        stop.set()
                        raise
                    finally:
                        _lane_stop.reset(token)
                # ThreadPool waits only for in-flight, non-cancellable calls to
                # return. Stop checkpoints prevent new nodes/builds/retries.
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[pool.submit(copy_context().run,search_lane,t) for t in ('prove','disprove')]
                    pairs=[f.result() for f in futures]
                plans=[p for p,_ in pairs if p is not None]
                outcomes=[out for _,out in pairs]
            for out in outcomes:
                failed_attempts.extend(out.get('failed_attempts',[]))
                if out.get('protocol_error'):
                    lane_protocol_errors.append(out['protocol_error'])
                history.extend(out['memory'])
                if out.get('abandoned'):
                    abandoned_lanes.append({'round':round_index,'task':out['task'],
                        'stage':out['abandon_stage'],'reason':out['abandon_reason'],
                        'trial':out['memory'][-1]['trial_index'] if out['memory'] else None})
            from .local_audit import emit
            emit('round_finished', round=round_index, plans=plans, outcomes=outcomes)
            rounds.append({'round':round_index,'plans':plans,'outcomes':[{k:v for k,v in out.items() if k!='memory'} for out in outcomes]})
            winners=[out for out in outcomes if out['success']]
            if len(winners)>1:
                conflict=True;break
            if winners:
                winning=winners[0];status='solved' if winning['task']=='prove' else 'false';break
        # A Repair failure and its completed build belong to the same attempt.
        attempts={(r['task'],r['strategy_round'],r['trial_index']) for r in history}
        attempts.update((r['task'],r['round'],r['trial']) for r in failed_attempts)
        prove_count=sum(t=='prove' for t,_,_ in attempts);disprove_count=sum(t=='disprove' for t,_,_ in attempts)
        audit=next(iter(self.memories.values())).path.with_name('goal_search_rounds.jsonl')
        audit.parent.mkdir(parents=True,exist_ok=True)
        with audit.open('a',encoding='utf-8') as f:
            f.write(json.dumps({'goals':goals,'mode':self.mode,'rounds':rounds,'validation_conflict':conflict,'lane_protocol_errors':lane_protocol_errors,'failed_attempts':failed_attempts},ensure_ascii=False,default=str)+'\n')
        if winning is None and lane_protocol_errors:
            raise GoalProtocolError('Budget ended without a Lean-verified solution; '
                'node contract failures remain execution errors, not Unsolved: '
                +json.dumps(lane_protocol_errors,ensure_ascii=False))
        return {'mode':self.mode,'success':status=='solved','final_status':status,
            'decision':'true' if status=='solved' else 'false' if status=='false' else 'unknown',
            'rationale':'Both goals verified; quarantined.' if conflict else 'Lean verified '+winning['task']+'.' if winning else 'Budget exhausted without a verified goal.',
            'trials':len(attempts),'proof_trials':prove_count,'disproof_trials':disprove_count,
            'falsity_opinions':0,'dual_truth_trials':prove_count,'dual_falsity_trials':disprove_count,
            'dual_falsity_opinions':0,'rounds':len(rounds),'trial_memory':history,
            'abandoned_lanes':abandoned_lanes,
            'verifier_result':winning['verification']['verifier_result'] if winning else {},
            'judge_result':{},'false_evidence':'lean_verified_negation' if status=='false' else 'none',
            'correction_to_true':'','goals':goals,'validation_conflict':conflict,
            'lane_protocol_errors':lane_protocol_errors,'failed_attempts':failed_attempts}
