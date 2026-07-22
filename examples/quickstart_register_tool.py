"""방안 A — @h.register_tool 데코레이터로 도구를 계측하는 최소 예제.

실행:
    python examples/quickstart_register_tool.py

examples/run_register_tool.jsonl에 tool_wrap + outcome 두 줄이 남는
것을 확인할 수 있다(*.jsonl은 .gitignore 대상이라 커밋되지 않는다).
"""

from pathlib import Path

from rein import Harness

RECORD_PATH = Path(__file__).parent / "run_register_tool.jsonl"

h = Harness(record=RECORD_PATH)


@h.register_tool
def add(a: int, b: int) -> int:
    return a + b


if __name__ == "__main__":
    print(add(2, 3))
    print(f"이벤트 로그: {RECORD_PATH}")
