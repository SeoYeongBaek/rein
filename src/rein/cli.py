"""rein CLI. CLAUDE.md §4: 공개 인터페이스로 취급, M1에서 확정.

명령어별 담당:
- seed:      골든 트레이스 녹화
- replay:    record/replay-verify/live-rerun 3모드
- rule-from: 실패 이벤트 → 규칙 생성
- report:    정적 report.html 렌더
"""

import warnings
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.table import Table

from rein.replay.engine import ReplayEngine, ReplayMismatchError

app = typer.Typer(name="rein", help="Agent = Model + Harness")
console = Console()


# §5: deny > approve > retry > allow
_VERDICT_PRIORITY = {"allow": 0, "retry": 1, "approve": 2, "deny": 3}


def _load_rules(rules_paths: list[str]) -> list[dict]:
    """rules.yaml 여러 개를 평탄화. 파일 하나에 `---`로 구분된 여러 규칙 문서도 지원
    (§4 rule-from의 append 동작을 받아내려면 한 파일에 규칙이 여러 개 쌓일 수 있음).

    TODO(가희): rule-from이 실제로 append할 때 멀티 문서(`---`)로 쓸지 `rules:` 리스트로
    쓸지 아직 미확정 — rule-from 구현 시 포맷이 확정되면 이 로더도 맞춰서 바꿀 것.
    """
    rules = []
    for path in rules_paths:
        text = Path(path).read_text(encoding="utf-8")
        for doc in yaml.safe_load_all(text):
            if doc and doc.get("rule"):
                rules.append(doc["rule"])
    return rules


def _rule_matches(rule: dict, evt: dict) -> bool:
    """when.tool + scope.agent.role 매칭.

    TODO(가희): when.features.class(DDL_DESTRUCTIVE 등) 매칭은 §7 featurizer
    (sqlglot)가 있어야 하는데 rules/__init__.py가 아직 빈 스텁이라 보류.
    """
    when = rule.get("when", {})
    if when.get("tool") and when.get("tool") != evt.get("tool_name"):
        return False

    scope = rule.get("scope") or {}
    scoped_role = scope.get("agent.role")
    if scoped_role and scoped_role != evt.get("context", {}).get("agent_role"):
        return False

    return True


def _verdict_from_rules(evt: dict, rules: list[dict]) -> str:
    """규칙 적용 후 판정. 매칭된 규칙이 여럿이면 §5 충돌 해결 우선순위
    (deny > approve > retry > allow)로 가장 제한적인 판정을 고른다.

    rules는 미리 _load_rules로 로드된 규칙 리스트 — 이벤트마다 파일을 다시
    읽지 않도록 호출자(_print_compare)가 루프 밖에서 한 번만 로드해서 넘긴다.

    TODO: §5 가드레일 4단계(schema/permission/budget/safety) 자체는 아직 없어서
    여기선 rules.yaml 매칭만 수행 — 가드레일 엔진 연결 후 교체.
    """
    matched = [rule.get("then", "allow") for rule in rules if _rule_matches(rule, evt)]
    if not matched:
        return "allow"
    return max(matched, key=lambda v: _VERDICT_PRIORITY.get(v, 0))


class ReplayMode(StrEnum):
    verify = "verify"
    live = "live"


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
    mode: Annotated[
        ReplayMode, typer.Option("--mode", help="verify=replay-verify(기본), live=live-rerun")
    ] = ReplayMode.verify,
    compare: Annotated[bool, typer.Option("--compare", help="가드레일 off/on A/B 비교")] = False,
):
    """녹화된 JSONL을 결정론적으로 재생한다 (record/replay-verify/live-rerun).

    TODO(현준): --mode live가 진짜 "실제 도구 재실행"은 아직 안 함 — Harness.register_tool
    (인터셉터)이 NotImplementedError라 재실행할 라이브 호출 소스 자체가 없다. 지금은
    verify와 동일하게 로그만 읽어서 보여주고 §6 경고만 추가로 찍는다.
    """
    log_path = Path(log)
    if not log_path.exists():
        typer.echo(f"오류: {log} 파일이 없습니다.", err=True)
        raise typer.Exit(1)

    # --mode live : live-rerun, 정직한 한계
    if mode == ReplayMode.live:
        typer.echo(
            "[경고] live-rerun 모드: 정직한 한계 - "
            "깨끗한 정량 A/B는 첫 개입 지점까지만 성립합니다. (CLAUDE.md §6)",
            err=True,
        )
    # --mode verify(기본)는 replay-verify, live는 live-rerun
    engine_mode = "replay-verify" if mode == ReplayMode.verify else "live-rerun"
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


# --compare 없이 replay 호출 시: 기록된 이벤트를 seq/tool_name/verdict/severity/detail로 나열
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


# --compare: recorded(§9 verdict, off) vs with_rules(_verdict_from_rules, on)를 나란히 비교
def _print_compare(events: list[dict], rules_paths: list[str]) -> None:
    table = Table(title="가드레일 A/B 비교 (off → on)", show_lines=False)
    table.add_column("seq", style="dim", width=5)
    table.add_column("tool_name")
    table.add_column("recorded (off)", style="dim")
    table.add_column("with_rules (on)")
    table.add_column("changed", width=8)

    rules = _load_rules(rules_paths)  # 루프 밖에서 한 번만 로드 (이벤트마다 재파싱하지 않음)
    changed_count = 0
    for evt in events:
        recorded = evt.get("verdict", "allow")
        with_rules = _verdict_from_rules(evt, rules)
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
    """실패 이벤트로부터 후보 규칙을 합성하고 회귀 검증 후 동결한다."""
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
