# 에이전트별 프롬프트

Single과 Dual의 메모리 사용 설정에서 실행 코드가 조합하는 **역할 지시문 원문**입니다. 읽기 편하도록 파일을 분리했으며, 실험에 사용되는 지시문을 새로 작성하거나 변경하지 않았습니다.

| 역할 | Single | Dual | 하는 일 |
| --- | --- | --- | --- |
| Orchestrator | [원문](single/orchestrator.txt) | [원문](dual/orchestrator.txt) | Single은 두 목표 중 하나를 선택하고 전략을 수립합니다. Dual은 지정된 각 목표에 대해 전략을 수립합니다. |
| Natural Agent | [원문](single/natural.txt) | [원문](dual/natural.txt) | 문제와 선택된 목표를 바탕으로 자연어 풀이를 작성합니다. |
| Lean Generator | [원문](single/lean.txt) | [원문](dual/lean.txt) | 전략과 자연어 풀이를 Lean 증명 코드로 작성합니다. |
| Repair Agent | [원문](single/repair.txt) | [원문](dual/repair.txt) | 검증 오류를 바탕으로 다음 코드 수정 지침을 작성합니다. |

Dual의 Natural Agent와 Lean Generator에는 목표에 따른 [role_assumption](dual/role_assumptions.json)도 입력으로 전달됩니다. 이는 탐색 방향을 지정하는 지시이며 Lean의 가정에 추가하지 않습니다.

## 실행 시 함께 전달되는 입력

| 역할 | 주요 입력 | 출력 |
| --- | --- | --- |
| Single Orchestrator | 문제, Prove·Disprove 목표, 이전 탐색 기록 | 선택한 목표와 Lean 전략(JSON) |
| Dual Orchestrator | 문제, 지정된 목표와 방향, 이전 라운드까지 양쪽 탐색 기록 | 지정된 목표와 Lean 전략(JSON) |
| Natural Agent | 문제와 선택된 목표 | 자연어 풀이 |
| Lean Generator | 목표, 자연어 풀이, Lean 전략, Lean 환경; 재시도 시 이전 코드·오류·수정 지침 | 증명 코드 줄 배열(JSON) |
| Repair Agent | 목표, 자연어 풀이, Lean 전략, 생성 코드와 검증 오류 | 수정 지침(JSON) |

Natural Agent에는 Orchestrator의 전략을 전달하지 않습니다. 한 라운드에서 만든 자연어 풀이는 그 라운드의 코드 수정 과정에서 유지됩니다. Dual의 두 경로는 같은 라운드에서 진행 중인 기록을 공유하지 않으며, 다음 라운드의 전략 수립 시 이전 양쪽 기록을 참고합니다. 정답 라벨과 정답 증명 코드는 추론 입력에 제공하지 않습니다.

## 원문과 실행 코드의 관계

위 파일은 고정 역할 지시문이며, 문제별 입력을 포함한 전체 API 요청은 아닙니다. 런타임은 지시문 뒤에 `Do not use tools, hidden labels or stored answers.`를 붙이고, 문제별 입력을 JSON으로 결합합니다. 설정에 따라 Lean 선언 정보와 수정 문맥이 추가될 수 있습니다.

- [지시문 원본](../src/single_orchestrator/goal_prompts.py)
- [입력 구성과 라운드 실행](../src/single_orchestrator/goal_search.py)
- [모델·예산 등 실행 조건](../REPRODUCIBILITY.md)

메모리 미사용 설정은 `planner_instruction(..., use_memory=False)`로 이전 기록 관련 지시를 제거합니다. 위 파일은 메모리 사용 설정을 보여줍니다.

지시문 원본을 수정한 경우 아래 명령으로 다시 내보낼 수 있습니다. `--check`는 파일이 현재 실행 코드와 일치하는지만 검사하며 모델을 호출하지 않습니다.

```bash
python scripts/export_prompts.py
python scripts/export_prompts.py --check
```
