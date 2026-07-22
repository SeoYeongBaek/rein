"""rein replay --compare / rein rule-from / rein report로 이어지는
CLI 워크플로용 run.jsonl을 생성하는 예제.

이 스크립트는 의도적으로 Harness(rules=...) 없이 실행한다 — 가드레일
off 상태로 기록해야 rein replay --compare가 off/on 차이를 보여줄 수
있다. replay-verify 자체는 실제 도구 호출이 필요 없어(CLAUDE.md §6)
CLI(`rein replay`)가 로그+rules만으로 단독 수행한다.

실행:
    python examples/replay_verify_workflow.py
    rein replay examples/run_workflow.jsonl --rules examples/rules.yaml \
        --compare
    rein rule-from examples/run_workflow.jsonl --event evt_0003 \
        -o examples/generated_rules.yaml --dry-run
    rein report examples/run_workflow.jsonl --rules examples/rules.yaml \
        -o examples/report.html
"""

from pathlib import Path

from rein import Harness

RECORD_PATH = Path(__file__).parent / "run_workflow.jsonl"

h = Harness(record=RECORD_PATH, context={"agent_role": "content_editor"})


@h.register_tool
def execute_sql(query: str) -> str:
    return f"executed: {query}"


if __name__ == "__main__":
    execute_sql(query="SELECT * FROM notices")
    execute_sql(query="UPDATE notices SET title = 'ok' WHERE id = 1")
    execute_sql(query="DROP TABLE users;")  # 가드레일 off라 그대로 실행됨

    print(f"이벤트 로그: {RECORD_PATH}")
    print("다음 명령으로 이어서 확인:")
    print(f"  rein replay {RECORD_PATH} --rules examples/rules.yaml --compare")
