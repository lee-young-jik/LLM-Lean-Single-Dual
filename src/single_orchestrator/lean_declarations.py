"""Shared, compiler-grounded library context; no benchmark proofs or labels.

Both architectures receive the same declarations for the same goal family.
The catalogue contains library APIs, never a problem-specific proof recipe.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
import re
from threading import Lock

from .local_audit import emit
from .verifier import _default_runner, _project_verification_lock


_LOCK = Lock()
_CACHE = {}
BASIS_APIS = (
    'Module.Basis', 'Module.Free.chooseBasis', 'Module.finBasis',
    'Module.finrank', 'Module.Basis.repr', 'Module.Basis.coord',
    'LinearMap.smulRight', 'Finsupp.ne_iff', "Submodule.eq_top_iff'",
)
LINEAR_APIS = ('LinearMap.injective_iff_surjective',)
# Query old spellings too, so a namespace error is not mistaken for an absent API.
IMPORT_SCOPE_QUERIES = ('Basis', 'FiniteDimensional.finBasis', 'finrank')


def declarations_for_goal(project_root, prelude, goals):
    """Select by goal text alone, independent of architecture or search results."""
    text = '\n'.join(goals.values()) if isinstance(goals, dict) else str(goals)
    symbols = []
    if any(cue in text for cue in ('Submodule', 'LinearIndependent', 'Basis')):
        symbols.extend(BASIS_APIS + IMPORT_SCOPE_QUERIES)
    if '→ₗ' in text and ('Function.Injective' in text or 'Function.Surjective' in text):
        symbols.extend(LINEAR_APIS)
    if not symbols:
        return None
    symbols = tuple(dict.fromkeys(symbols))
    root = Path(project_root).resolve()
    evidence = {'prelude': prelude, 'symbols': symbols, 'schema': 2}
    for name in ('lean-toolchain', 'lake-manifest.json'):
        evidence[name] = (root / name).read_text()
    mathlib_olean = root / '.lake/packages/mathlib/.lake/build/lib/lean/Mathlib.olean'
    if mathlib_olean.exists():
        stat = mathlib_olean.stat()
        evidence['mathlib_olean'] = [stat.st_size, stat.st_mtime_ns]
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    key = (str(root), digest)
    with _LOCK:
        if key not in _CACHE:
            directory = root / '.dual_arc_declarations'
            directory.mkdir(exist_ok=True)
            target = directory / (digest + '.json')
            with (directory / '.cache.lock').open('a+') as gate:
                fcntl.flock(gate.fileno(), fcntl.LOCK_EX)
                try:
                    if target.exists():
                        data = json.loads(target.read_text())
                        if data.get('environment_sha256') != digest:
                            raise RuntimeError('Lean declaration cache fingerprint mismatch')
                    else:
                        data = _probe(root, prelude, symbols, digest, directory)
                        temp = target.with_suffix('.tmp')
                        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
                        temp.replace(target)
                finally:
                    fcntl.flock(gate.fileno(), fcntl.LOCK_UN)
            _CACHE[key] = data
        data = _CACHE[key]
    emit('lean_declaration_context', environment_sha256=digest,
         symbols=symbols, available=[d['name'] for d in data['declarations']])
    return data


def _probe(root, prelude, symbols, digest, directory):
    source = directory / (digest + '.lean')
    lines = [prelude]
    for symbol in symbols:
        lines += [f'#eval IO.println "DECL_BEGIN:{symbol}"',
                  f'#check @{symbol}', f'#eval IO.println "DECL_END:{symbol}"']
    source.write_text('\n'.join(lines) + '\n')
    with _project_verification_lock(root):
        result = _default_runner(['lake', 'env', 'lean', str(source)], root, 120)
    output = result.stdout + '\n' + result.stderr
    if result.returncode < 0 or 'unknown module prefix' in output:
        raise RuntimeError('Lean declaration inspection failed: ' + output[-1500:])
    declarations = []
    unresolved = []
    for symbol in symbols:
        start, end = f'DECL_BEGIN:{symbol}', f'DECL_END:{symbol}'
        if start not in output or end not in output:
            raise RuntimeError('Incomplete Lean declaration inspection: ' + symbol)
        block = output.split(start, 1)[1].split(end, 1)[0].strip()
        signature = re.search(r'(?m)^@?' + re.escape(symbol) + r'(?:\.\{[^}]*\})?\s*:', block)
        if (not signature or re.search(r'\berror(?:\([^)]*\))?:', block)):
            unresolved.append({'name': symbol, 'diagnostic': block[:1200]})
        else:
            declarations.append({'name': symbol, 'type': block[signature.start():]})
    return {'source': 'Lean #check under the same verifier prelude; library declarations only',
            'environment_sha256': digest, 'declarations': declarations,
            'unresolved_names_at_import_scope': unresolved}
