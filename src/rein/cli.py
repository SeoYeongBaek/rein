"""rein CLI. CLAUDE.md §4: 공개 인터페이스로 취급, M1에서 확정.

명령어별 담당:
- seed:      골든 트레이스 녹화
- replay:    record/replay-verify/live-rerun 3모드
- rule-from: 실패 이벤트 → 규칙 생성
- report:    정적 report.html 렌더
"""

from __future__ import annotations

import warnings
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from rein.replay.engine import ReplayEngine, ReplayMismatchError

app = typer.Typer(name="rein", help="Agent = Model + Harness")
console = Console()


def _verdict_from_rules(evt: dict, rules_paths: list[str]) -> str:
    """규칙 적용 후 판정. when.tool 매칭만 수행하는 최소 구현.
    TODO: 가드레일 파이프라인 연결 후 교체 (M1 가드레일 구현 시).
    """
    for path in rules_paths:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        rule = data.get("rule", {})
        if rule.get("when", {}).get("tool") == evt.get("tool_name"):
            return rule.get("then", "allow")
    return "allow"


@app.command()
def seed(output: str = typer.Option("golden_run.jsonl", "-o", "--output")):
    """정상 시나리오를 녹화해 골든 코퍼스를 만든다 (강력 권장, 필수 아님)."""
    raise NotImplementedError


@app.command()
def replay(
    log: str = typer.Argument(..., help="녹화된 JSONL 파일 경로"),  # noqa: B008
    rules: list[str] | None = typer.Option(  # noqa: B008
        None, "--rules", help="규칙 YAML 파일 (여러 개 가능)"
    ),
    mode: str = typer.Option("verify", "--mode", help="verify(기본) | live"),  # noqa: B008
    compare: bool = typer.Option(False, "--compare", help="가드레일 off/on A/B 비교"),  # noqa: B008
):
    """녹화된 JSONL을 결정론적으로 재생한다 (record/replay-verify/live-rerun)."""
    if mode not in ("verify", "live"):
        typer.echo(f"오류: --mode는 verify 또는 live여야 합니다. (입력: {mode!r})", err=True)
        raise typer.Exit(1)

    log_path = Path(log)
    if not log_path.exists():
        typer.echo(f"오류: {log} 파일이 없습니다.", err=True)
        raise typer.Exit(1)

    if mode == "live":
        # §6 정직한 한계 경고
        typer.echo(
            "[경고] live-rerun 모드: 정직한 한계 - "
            "깨끗한 정량 A/B는 첫 개입 지점까지만 성립합니다. (CLAUDE.md §6)",
            err=True,
        )

    engine_mode = "replay-verify" if mode == "verify" else "live-rerun"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # CLI에서 이미 출력
        try:
            engine = ReplayEngine(log_path, mode=engine_mode)
        except ReplayMismatchError as e:
            typer.echo(f"오류: {e}", err=True)
            raise typer.Exit(1) from None

    events = list(engine)
    if not events:
        typer.echo("tool_wrap 이벤트가 없습니다.")
        return

    if compare:
        _print_compare(events, rules or [])
    else:
        _print_events(events)


def _print_events(events: list[dict]) -> None:
    table = Table(title="replay 결과", show_lines=False)
    table.add_column("seq", style="dim", width=5)
    table.add_column("tool_name")
    table.add_column("verdict")
    table.add_column("severity")
    table.add_column("detail")

    for evt in events:
        outcome = evt.get("outcome") or {}
        severity = outcome.get("severity", "-")
        severity_style = {"critical": "red", "warning": "yellow", "info": "green"}.get(severity, "")
        table.add_row(
            str(evt.get("seq", "-")),
            evt.get("tool_name", "-"),
            evt.get("verdict", "-"),
            f"[{severity_style}]{severity}[/{severity_style}]",
            outcome.get("detail", "-"),
        )

    console.print(table)


def _print_compare(events: list[dict], rules_paths: list[str]) -> None:
    table = Table(title="가드레일 A/B 비교 (off → on)", show_lines=False)
    table.add_column("seq", style="dim", width=5)
    table.add_column("tool_name")
    table.add_column("recorded (off)", style="dim")
    table.add_column("with_rules (on)")
    table.add_column("changed", width=8)

    changed_count = 0
    for evt in events:
        recorded = evt.get("verdict", "allow")
        with_rules = _verdict_from_rules(evt, rules_paths)
        changed = recorded != with_rules
        if changed:
            changed_count += 1

        table.add_row(
            str(evt.get("seq", "-")),
            evt.get("tool_name", "-"),
            recorded,
            f"[red]{with_rules}[/red]" if changed else with_rules,
            "[red]CHANGED[/red]" if changed else "-",
        )

    console.print(table)
    console.print(f"\n총 {len(events)}개 이벤트 중 [red]{changed_count}개[/red] 판정 변경")


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
