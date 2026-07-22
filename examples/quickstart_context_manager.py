"""방안 B — 컨텍스트 매니저로 하네스 수명주기를 감싸는 예제.

도구는 register_tool로 미리 등록되어 있어야 한다 — with 블록은
자동으로 도구 호출을 가로채지 않는다(CLAUDE.md §4 정정 참고). with는
스테이지 확정(seal)과 이벤트 저장소 close만 관리한다.

실행:
    python examples/quickstart_context_manager.py
"""

from pathlib import Path

from rein import Harness

RECORD_PATH = Path(__file__).parent / "run_context_manager.jsonl"

h = Harness(record=RECORD_PATH)


@h.register_tool
def delete_file(path: str) -> str:
    return f"deleted: {path}"


def agent_loop() -> None:
    print(delete_file(path="/tmp/scratch.txt"))


if __name__ == "__main__":
    with h:
        agent_loop()
    print(f"이벤트 로그: {RECORD_PATH}")
