"""Validate the self-contained 137-problem release without model/Lean calls."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data/book137_v1'

def read(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

def main():
    manifest = json.loads((DATA / 'manifest.json').read_text())
    for name, expected in manifest['sha256'].items():
        assert hashlib.sha256((DATA / name).read_bytes()).hexdigest() == expected, name
    problems = read(DATA / 'problems.jsonl')
    labels = {r['id']: r for r in read(DATA / 'labels.jsonl')}
    refs = {r['id']: r for r in read(DATA / 'reference_answers.jsonl')}
    ids = {r['name'] for r in problems}
    assert len(problems) == len(ids) == len(labels) == len(refs) == 137
    assert ids == set(labels) == set(refs) == set((DATA / 'ids.txt').read_text().splitlines())
    assert sum(r['truth_label'] for r in labels.values()) == 62
    forbidden = {'truth_label', 'verified_task', 'lean_proof', 'lean_code', 'certificate',
                 'gold_proof_lines', 'gold_length_class', 'selection'}
    assert all(not forbidden.intersection(r) for r in problems)
    assert all(r['split'] == 'book137_v1' for r in problems)
    for i in ids:
        assert refs[i]['truth_label'] == labels[i]['truth_label']
        assert refs[i]['verified_task'] == labels[i]['verified_task']
        assert refs[i]['lean_code'] and labels[i]['gold_proof_lines'] > 0
    print('Validated 137 inputs, 62 Prove/75 Disprove, separate reference answers and dataset hashes.')
    print('No model calls or new Lean certification performed.')

if __name__ == '__main__':
    main()
