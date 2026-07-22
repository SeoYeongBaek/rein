# rein examples

각 스크립트는 리포지토리 루트에서 그대로 실행 가능하다
(`pip install -e .` 이후). `demo/ab_demo/`는 별도의 대회 발표용 A/B
데모 자산이며, 여기 예제와는 독립적으로 유지된다.

| 스크립트 | 보여주는 것 |
|---|---|
| `quickstart_register_tool.py` | 방안 A — `@h.register_tool` 데코레이터 최소 통합 |
| `quickstart_context_manager.py` | 방안 B — `with h:`로 하네스 수명주기 감싸기(도구는 여전히 `register_tool`로 먼저 등록) |
| `guardrail_rule_yaml.py` (+ `rules.yaml`) | `Harness(rules=...)`가 라이브 실행 중 파괴적 SQL을 실제로 막는 예제 |
| `replay_verify_workflow.py` | `run.jsonl`을 생성해 `rein replay --compare` / `rein rule-from` / `rein report` CLI로 이어가는 워크플로 |

## 실행

```bash
python examples/quickstart_register_tool.py
python examples/quickstart_context_manager.py
python examples/guardrail_rule_yaml.py
python examples/replay_verify_workflow.py
rein replay examples/run_workflow.jsonl --rules examples/rules.yaml --compare
```

실행 중 생성되는 `*.jsonl`/`*.html` 산출물은 `.gitignore`로 이미
제외되어 커밋되지 않는다.
