"""Durable local evidence independent of LangSmith; never part of model inputs."""
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import time
import uuid

SESSION = ContextVar('local_audit_session', default=None)
NODE = ContextVar('local_audit_node', default=None)


def now():
    return datetime.now(timezone.utc).isoformat()


def safe(value):
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if any(s in k.lower() for s in
                ('api_key', 'authorization', 'password', 'secret', 'access_token')) else safe(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(x) for x in value]
    if hasattr(value, 'model_dump'):
        return safe(value.model_dump(mode='json'))
    if isinstance(value, str):
        # Errors may contain configured credentials. Do not copy environment wholesale.
        for key, credential in os.environ.items():
            if any(s in key.upper() for s in ('API_KEY', 'PASSWORD', 'SECRET', 'ACCESS_TOKEN')) and len(credential) >= 8:
                value = value.replace(credential, '[REDACTED]')
    return value


def emit(event, **payload):
    session = SESSION.get()
    if session is not None:
        session.emit(event, **payload)


class AuditSession:
    def __init__(self, parent, manifest):
        self.id = uuid.uuid4().hex
        self.path = Path(parent) / 'audit' / self.id
        self.path.mkdir(parents=True, exist_ok=False)
        self.lock = RLock()
        self.sequence = 0
        self.started = time.monotonic()
        self.write('manifest.json', {'schema_version': 1, 'run_id': self.id,
                   'started_utc': now(), **manifest})
        source_dir = self.path / 'source_snapshot'
        source_dir.mkdir()
        for name in ('goal_search.py', 'goal_prompts.py', 'planner_memory.py', 'goal_api.py',
                     'model.py', 'verifier.py', 'formal_false_review.py', 'proof_audit.py', 'local_audit.py', 'goal_trace.py', 'lean_declarations.py', 'repair_context.py'):
            source = Path(__file__).with_name(name)
            (source_dir/name).write_bytes(source.read_bytes())
        for name in ('lean-toolchain', 'lake-manifest.json'):
            source = Path(manifest.get('verifier', {}).get('project_root', '.'))/name
            if source.exists(): (source_dir/name).write_bytes(source.read_bytes())
        self.emit('run_started')

    def write(self, name, payload):
        with self.lock:
            target = self.path / name
            temp = target.with_suffix(target.suffix + '.tmp')
            with temp.open('w', encoding='utf-8') as stream:
                json.dump(safe(payload), stream, ensure_ascii=False, indent=2, default=str)
                stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
            temp.replace(target)

    def emit(self, event, **payload):
        with self.lock:
            self.sequence += 1
            item = safe({'sequence': self.sequence, 'run_id': self.id,
                         'node_id': NODE.get(), 'utc': now(),
                         'elapsed_seconds': time.monotonic() - self.started,
                         'event': event, **payload})
            with (self.path / 'events.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(item, ensure_ascii=False, default=str) + '\n')
                stream.flush(); os.fsync(stream.fileno())

    def finish(self, status, **payload):
        self.write('outcome.json', {'run_id': self.id, 'execution_status': status,
                   'finished_utc': now(), 'wall_seconds': time.monotonic()-self.started, **payload})
        self.emit('run_finished', execution_status=status)
        # Readable view is derived from complete JSONL, never used by the model.
        lines = ['# Local execution audit', '', f'Run: {self.id}', '']
        for line in (self.path/'events.jsonl').read_text().splitlines():
            event = json.loads(line)
            if event['event'] not in ('node_started','node_output','node_error','api_error'):
                continue
            lines.extend([f"## {event['sequence']} · {event['event']} · {event.get('name', '')}",
                          '', '```json', json.dumps(event, ensure_ascii=False, indent=2), '```', ''])
        (self.path/'trace.md').write_text('\n'.join(lines), encoding='utf-8')


def environment(verifier, model_client):
    root = Path(__file__).resolve().parents[2]
    files = list(Path(__file__).parent.glob('*.py'))
    project = Path(getattr(verifier, 'project_root', root))
    files += [p for p in (project/'lean-toolchain', project/'lake-manifest.json') if p.exists()]
    def client_info(client):
        return {'client': type(client).__name__, **{k: getattr(client, k) for k in
                ('model', 'timeout_seconds', 'base_url') if hasattr(client, k)}}
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    versions = {}
    for package in ('openai', 'langsmith', 'langgraph', 'python-dotenv'):
        try: versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError: pass
    return {'python': platform.python_version(), 'platform': platform.platform(),
            'packages': versions, 'git_commit': commit,
            'sampling_note': 'Exact supplied generation options appear in api_request; omitted options use provider defaults. Ordering seed is not a model sampling seed.',
            'source_hashes': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
            'models': {'default': client_info(getattr(model_client, 'default_client', model_client)),
                       'routes': {k: client_info(v) for k,v in getattr(model_client, 'prompt_clients', {}).items()}},
            'generation_settings': {k: v for k,v in os.environ.items() if k in {
                'DUAL_ARC_MAX_OUTPUT_TOKENS', 'DUAL_ARC_API_MAX_ATTEMPTS',
                'DUAL_ARC_API_MAX_TRANSPORT_ATTEMPTS', 'DUAL_ARC_OPENROUTER_PROVIDER',
                'DUAL_ARC_REPAIR_CONTEXT', 'DUAL_ARC_LEAN_DECLARATIONS',
                'DUAL_ARC_OPENROUTER_MODEL_PROVIDERS'}},
            'verifier': {k: str(getattr(verifier,k)) for k in
                ('project_root','module_name','lean_file','prelude','timeout_seconds','strict_timeout_guard') if hasattr(verifier,k)}}
