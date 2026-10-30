"""Export every diagnostic JSON/JSONL log as readable Markdown, optionally live."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time


def fenced(value: str, language: str = '') -> str:
    fence = '~~~~'
    while fence in value:
        fence += '~'
    return f'{fence}{language}\n{value}\n{fence}\n'


def export_logs(root: Path) -> None:
    for source in sorted([*root.rglob('*.json'), *root.rglob('*.jsonl')]):
        try:
            if source.suffix == '.jsonl':
                rows = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
            else:
                rows = [json.loads(source.read_text())]
        except (json.JSONDecodeError, OSError):
            continue  # A concurrent writer may be midway through the latest line.
        parts = [f'# {source.name}\n\n', '원본 기록 전체를 변환했습니다. 프롬프트와 응답을 생략하지 않습니다.\n\n']
        for index, row in enumerate(rows, 1):
            parts.append(f'## {index}. {row.get("prompt_name", row.get("event", "기록"))}\n\n')
            for key, value in row.items():
                parts.append(f'### {key}\n\n')
                if isinstance(value, str):
                    parts.append(fenced(value))
                else:
                    parts.append(fenced(json.dumps(value, ensure_ascii=False, indent=2), 'json'))
                parts.append('\n')
        source.with_suffix('.md').write_text(''.join(parts))
    comparison = ['# False Reasoner 응답과 최종 판정 비교\n\n',
                  '전체 원시 프롬프트·응답은 각 모드의 `model_calls.md`에 보존됩니다. '
                  '아래에는 파싱된 진단과 최종 검사 결과를 생략 없이 표시합니다.\n\n']
    for mode in ('single', 'dual'):
        comparison.append(f'## {mode.title()}\n\n')
        path = root / mode / 'false_decisions.jsonl'
        if not path.exists():
            result = root / mode / 'results.jsonl'
            finished_solved = False
            try:
                finished_solved = json.loads(result.read_text().splitlines()[0])['final_status'] == 'solved'
            except (OSError, IndexError, KeyError, json.JSONDecodeError):
                pass
            comparison.append(('Lean 검증 성공으로 종료되어 False Reasoner와 거짓 판정 단계가 호출되지 않았습니다.'
                               if finished_solved else '아직 거짓 판정 기록이 생성되지 않았습니다.') + '\n\n')
            continue
        try:
            decisions = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        except json.JSONDecodeError:
            continue
        for number, decision in enumerate(decisions, 1):
            comparison.append(f'### 판정 {number}\n\n')
            claim = decision['claim']
            if 'lean_proof' in claim:
                comparison.append('**Lean 반증 본문**\n\n' + fenced(claim['lean_proof'], 'lean') + '\n')
                comparison.append('**고정 대상·검증 결과 전체**\n\n' +
                                  fenced(json.dumps(decision['result'], ensure_ascii=False, indent=2), 'json') + '\n')
            if 'is_false' in claim:
                fields = [('거짓 여부 (is_false)', str(claim['is_false'])),
                          ('수학적 이유 (reason)', claim.get('reason', ''))]
            else:
                fields = [('진단 이유 (why)', claim.get('why', '')),
                          ('수정 제안 (change)', claim.get('change', '')),
                          ('불확실 여부 (unclear)', str(claim.get('unclear', True)))]
            fields += [('최종 판정', decision['result']['decision']),
                       ('최종 판정 이유', decision['result']['why'])]
            for label, value in fields:
                comparison.append(f'**{label}**\n\n{fenced(value)}\n')
    (root / 'false_review_comparison.md').write_text(''.join(comparison))
    files = sorted(root.rglob('*.md'))
    parts = ['# 문제 재실행 기록\n\n',
             '모델 교체 후의 진단용 추가 실험입니다. 기존 논문 실험과 구분합니다.\n\n',
             '각 파일에는 원본 로그의 전체 내용이 들어 있습니다.\n\n']
    for path in files:
        if path.name != 'README.md':
            relative = path.relative_to(root).as_posix()
            parts.append(f'- [{relative}]({relative})\n')
    (root / 'README.md').write_text(''.join(parts))


def main() -> None:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    while True:
        export_logs(args.directory)
        result = args.directory / 'dual/results.jsonl'
        if not args.watch or (result.exists() and result.stat().st_size > 0):
            break
        time.sleep(2)


if __name__ == '__main__':
    main()
