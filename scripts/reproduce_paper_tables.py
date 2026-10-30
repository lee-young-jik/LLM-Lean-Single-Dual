"""Recompute the final manuscript tables from archived records; no model calls."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import statistics

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data/book137_v1'
RESULTS = ROOT / 'results/final_paper'


def read(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def proof_lines(code):
    code = re.sub(r'/-.*?-/', '', code, flags=re.S)
    return sum(bool(line.split('--', 1)[0].strip()) for line in code.splitlines())


def compute():
    manifest = json.loads((RESULTS / 'manifest.json').read_text())
    for name, digest in manifest['sha256'].items():
        assert hashlib.sha256((RESULTS / name).read_bytes()).hexdigest() == digest, name
    labels = {r['id']: r for r in read(DATA / 'labels.jsonl')}
    rows = read(RESULTS / 'executions.jsonl')
    choices = read(RESULTS / 'single_choices.jsonl')
    ids = set(labels)
    assert len(ids) == 137 and len(rows) == 1370
    assert {(r['id'], r['mode'], r['logical_repetition']) for r in rows} == {
        (i, m, k) for i in ids for m in ('single', 'dual') for k in range(1, 6)}
    assert len({r['run_id'] for r in rows}) == len(rows)
    for r in rows:
        assert r['expected_task'] == labels[r['id']]['verified_task']
        assert r['correct'] == (r['prediction'] == r['expected_task'])
        assert r['prediction'] in (r['expected_task'], 'unsolved')
        assert 1 <= r['rounds'] <= 3
        assert r['input_tokens'] + r['output_tokens'] == r['total_tokens']
        assert r['missing_usage_responses'] >= 0
        if r['correct']:
            assert r['lean_proof'] and proof_lines(r['lean_proof']) == r['model_lines']
        else:
            assert r['lean_proof'] is None and r['model_lines'] is None
    first = {}
    for i in ids:
        for m in ('single', 'dual'):
            runs = sorted((r for r in rows if r['id'] == i and r['mode'] == m),
                          key=lambda r: r['logical_repetition'])
            first[i, m] = next((r for r in runs if r['correct']), None)
    ordered = sorted(ids, key=lambda i: (labels[i]['gold_proof_lines'], i))
    groups = {'Short': ordered[:46], 'Medium': ordered[46:92],
              'Long': ordered[92:], 'Overall': ordered}
    report = {'table1': {}, 'table4': {}, 'table5': [], 'table6': {}, 'table7': {}}
    for book in sorted({r['book_key'] for r in labels.values()}):
        subset = [r for r in labels.values() if r['book_key'] == book]
        report['table1'][book] = {'prove': sum(r['truth_label'] for r in subset),
                                'disprove': sum(not r['truth_label'] for r in subset),
                                'total': len(subset)}
    for m in ('single', 'dual'):
        rs = [r for r in rows if r['mode'] == m]
        wins = [first[i, m] for i in ids if first[i, m]]
        counts = Counter(r['prediction'] for r in wins)
        by_rep = [sum(r['correct'] and r['logical_repetition'] == k for r in rs)
                  for k in range(1, 6)]
        report['table4'][m] = {
            'proved': counts['prove'], 'disproved': counts['disprove'],
            'unsolved': 137 - len(wins), 'pass_at_5_solved': len(wins),
            'pass_at_5_percent': len(wins) / 137 * 100,
            'accuracy_at_5_successes': sum(by_rep),
            'accuracy_at_5_denominator': 685,
            'accuracy_at_5_percent': sum(by_rep) / 685 * 100,
            'per_repetition_solved': by_rep,
            'mean_solved': statistics.mean(by_rep), 'sample_sd_solved': statistics.stdev(by_rep)}
        report['table6'][m] = [sum(r['rounds'] == n for r in wins) for n in (1, 2, 3)]
        report['table7'][m] = {k: statistics.mean(r[k] for r in rs) for k in (
            'input_tokens', 'output_tokens', 'total_tokens', 'wall_seconds', 'reported_cost_usd')}
    report['usage_missing_responses'] = {m: sum(r['missing_usage_responses'] for r in rows if r['mode'] == m) for m in ('single', 'dual')}
    for group, members in groups.items():
        row = {'group': group, 'n': len(members)}
        for m in ('single', 'dual'):
            wins = [first[i, m] for i in members if first[i, m]]
            row[m] = {'solved': len(wins), 'length_ratio': statistics.mean(
                r['model_lines'] / labels[r['id']]['gold_proof_lines'] for r in wins)}
        report['table5'].append(row)
    single_only = sum(first[i, 'single'] is not None and first[i, 'dual'] is None for i in ids)
    dual_only = sum(first[i, 'dual'] is not None and first[i, 'single'] is None for i in ids)
    from math import comb
    discordant = single_only + dual_only
    p = min(1., 2 * sum(comb(discordant, j) for j in range(min(single_only, dual_only)+1))
            / (2 ** discordant)) if discordant else 1.
    report['paired_pass_at_5'] = {'both': sum(all(first[i, m] for m in ('single','dual')) for i in ids),
                                'single_only': single_only, 'dual_only': dual_only,
                                'mcnemar_exact_two_sided_p': p}
    single_rows = {r['run_id']: r for r in rows if r['mode'] == 'single'}
    for c in choices:
        r = single_rows[c['run_id']]
        assert (c['id'], c['logical_repetition']) == (r['id'], r['logical_repetition'])
        assert c['correct_direction'] == (c['selected_task'] == r['expected_task'])
    assert len({(c['run_id'], c['round']) for c in choices}) == len(choices)
    for run_id, r in single_rows.items():
        assert sorted(c['round'] for c in choices if c['run_id'] == run_id) == list(range(1, r['rounds']+1))
    wrong = [c for c in choices if not c['correct_direction']]
    report['single_wrong_direction'] = {'total': len(wrong),
        'by_round': {str(n): sum(c['round'] == n for c in wrong) for n in (1, 2, 3)}}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Compare recomputation with published tables.json')
    parser.add_argument('--output', type=Path, help='Write recomputed JSON to a separate file')
    args = parser.parse_args()
    result = compute()
    if args.check:
        expected = json.loads((RESULTS / 'tables.json').read_text())
        assert result == expected, 'Published tables differ from archived records'
        paper = (ROOT / 'paper/paper.tex').read_text()
        assert 'Single & 50/56/31 & 77.37 & 62.77' in paper
        assert 'Dual & 52/57/28 & 79.56 & 65.55' in paper
        for group in result['table5']:
            a, b = group['single'], group['dual']
            needle = f"{group['group']} & {a['solved']}/{group['n']} & {a['length_ratio']:.2f} & {b['solved']}/{group['n']} & {b['length_ratio']:.2f}"
            assert needle in paper, needle
        for m in ('single','dual'):
            assert m.title() + ' & ' + ' & '.join(map(str, result['table6'][m])) in paper
        print('PASS: 1370 archived executions, proof lengths, final tables 1/4/5/6/7 and Single direction choices.')
        print('No model calls or new Lean verification.')
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    elif not args.check:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
