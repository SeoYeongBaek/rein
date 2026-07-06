"""rein CLI. CLAUDE.md §4: 공개 인터페이스로 취급, M1에서 확정.

명령어별 담당:
- seed:      골든 트레이스 녹화
- replay:    record/replay-verify/live-rerun 3모드
- rule-from: 실패 이벤트 → 규칙 생성
- report:    정적 report.html 렌더
"""

from typing import Annotated

import typer

app = typer.Typer(name="rein", help="Agent = Model + Harness")


@app.command()
def seed(
    run_log: Annotated[
        str, typer.Argument(help="검증할 run.jsonl 경로 (Harness(record=...)로 녹화됨)")
    ],
):
    """스키마 + critical outcome 0건을 검증한 뒤 golden_run.jsonl로 지정한다."""
    raise NotImplementedError


@app.command()
def replay(
    log: str,
    rules: Annotated[
        list[str] | None, typer.Option("--rules", help="적용할 rules.yaml (반복 지정 가능)")
    ] = None,
    compare: Annotated[bool, typer.Option("--compare", help="가드레일 off/on A/B 비교")] = False,
):
    """녹화된 JSONL을 replay-verify로 재생한다 (CLAUDE.md §4/§6).

    live-rerun은 이 명령의 옵션이 아니다. 실제 도구 함수는 사용자
    프로세스 안에만 존재해 로그 파일만 받는 CLI가 대신 실행할 수 없다.
    live-rerun이 필요하면 사용자 스크립트 안에서
    Harness(mode="live-rerun", replay_from=log)로 직접 트리거한다.
    """
    raise NotImplementedError


@app.command(name="rule-from")
def rule_from(
    log: str,
    event: Annotated[str, typer.Option("--event", help="예: evt_0042")],
    golden: Annotated[
        str | None,
        typer.Option("--golden", help="없으면 §7 콜드 스타트 안전장치 ②(합성 음성)를 사용"),
    ] = None,
    output: Annotated[str, typer.Option("-o", "--output")] = "rules.yaml",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="후보 규칙과 회귀 매트릭스만 출력, 파일에 쓰지 않음")
    ] = False,
):
    """실패 이벤트로부터 후보 규칙을 합성하고 회귀 검증 후 동결한다.

    이슈 #11 확정: --golden 미지정 시 합성 음성(§7 안전장치 ②) 도출 조건.
    run.jsonl의 tool_wrap 이벤트 중 다음을 모두 만족하면 음성 후보:
      - tool_name == born_from.tool_name (다른 도구는 §7 빔서치가
        tool_name을 최상위 특징으로 고정하므로 회귀 검증에 신호를 주지
        못하는 가짜 음성이라 제외)
      - verdict == "allow"
      - outcome.severity == "info" (class 기반 산출값이라 이 필터
        하나로 §8 "validated_against는 음성 전용" 요구가 충족됨 —
        다른 role의 DROP TABLE 같은 위험 호출이 섞여 들어올 수 없음)
      - evt != born_from.evt
      - agent.role은 필터 조건 아님 (같은 role의 무해한 호출, 다른
        role의 같은 도구 호출 둘 다 유효한 음성)
    후보가 0건이면 일반화하지 않고 빔서치 깊이 3(tool_name+class+
    agent.role 모두 born_from 값 고정)인 가장 좁은 후보만 채택 —
    실제 게이팅 구현은 이슈 #10(synthesize & verify) 쪽에서.
    """
    raise NotImplementedError


@app.command()
def report(
    log: str,
    rules: Annotated[
        str, typer.Option("--rules", help="필수 — 후보/채택 규칙 회귀 매트릭스 렌더용")
    ],
    output: Annotated[str, typer.Option("-o", "--output")] = "report.html",
):
    """분기 타임라인/지표/후보 회귀 표/규칙 회귀 매트릭스를 담은 정적 HTML을 만든다."""
    raise NotImplementedError


if __name__ == "__main__":
    app()
