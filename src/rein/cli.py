"""rein CLI. CLAUDE.md §4: 공개 인터페이스로 취급, M1에서 확정.

명령어별 담당:
- seed:      골든 트레이스 녹화
- replay:    record/replay-verify/live-rerun 3모드
- rule-from: 실패 이벤트 → 규칙 생성
- report:    정적 report.html 렌더
"""

import typer

app = typer.Typer(name="rein", help="Agent = Model + Harness")


@app.command()
def seed(output: str = typer.Option("golden_run.jsonl", "-o", "--output")):
    """정상 시나리오를 녹화해 골든 코퍼스를 만든다 (강력 권장, 필수 아님)."""
    raise NotImplementedError


@app.command()
def replay(
    log: str,
    compare: bool = typer.Option(False, "--compare", help="가드레일 off/on A/B 비교"),
):
    """녹화된 JSONL을 결정론적으로 재생한다 (record/replay-verify/live-rerun)."""
    raise NotImplementedError


@app.command(name="rule-from")
def rule_from(
    log: str,
    event: str = typer.Option(..., "--event", help="예: evt_0042"),
    output: str = typer.Option("rules.yaml", "-o", "--output"),
):
    """실패 이벤트로부터 후보 규칙을 합성하고 회귀 검증 후 동결한다."""
    raise NotImplementedError


@app.command()
def report(
    log: str,
    output: str = typer.Option("report.html", "-o", "--output"),
):
    """분기 타임라인/지표/후보 회귀 표/규칙 회귀 매트릭스를 담은 정적 HTML을 만든다."""
    raise NotImplementedError


if __name__ == "__main__":
    app()
