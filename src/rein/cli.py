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


# TODO(현준/가희): §4 명세 대비 시그니처부터 어긋남 — 스펙은 `rein seed <run.jsonl>`로
# 이미 녹화된 로그를 "검증"(schema + critical outcome 0건)만 하고 golden_run.jsonl로
# 지정하는 건데, 지금은 입력 로그 인자 자체가 없다. 본문도 아직 NotImplementedError.
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
    # 기본값 verify
    mode: str = typer.Option("verify", "--mode", help="verify(기본) | live"),  # noqa: B008
    compare: bool = typer.Option(False, "--compare", help="가드레일 off/on A/B 비교"),  # noqa: B008
):
    """녹화된 JSONL을 결정론적으로 재생한다 (record/replay-verify/live-rerun).

    TODO(현준): --mode live가 진짜 "실제 도구 재실행"은 아직 안 함 — Harness.register_tool
    (인터셉터)이 NotImplementedError라 재실행할 라이브 호출 소스 자체가 없다. 지금은
    verify와 동일하게 로그만 읽어서 보여주고 §6 경고만 추가로 찍는다.
    """
    # 잘못된 mode 는 exit 1 실행
    if mode not in ("verify", "live"):
        typer.echo(f"오류: --mode는 verify 또는 live여야 합니다. (입력: {mode!r})", err=True)
        raise typer.Exit(1)

    log_path = Path(log)
    if not log_path.exists():
        typer.echo(f"오류: {log} 파일이 없습니다.", err=True)
        raise typer.Exit(1)

    # --mode live : live-rerun, 정직한 한계
    if mode == "live":
        typer.echo(
            "[경고] live-rerun 모드: 정직한 한계 - "
            "깨끗한 정량 A/B는 첫 개입 지점까지만 성립합니다. (CLAUDE.md §6)",
            err=True,
        )
    # --mode verify`(기본)는 replay-verify
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


# TODO(가희): §4 명세 대비 시그니처에 --golden, --dry-run 옵션이 빠져 있음.
# 본문(featurize → 후보 합성 → 회귀 검증, §7 계층적 빔서치 K=8/depth=3)은
# rules/__init__.py가 빈 스텁이라 아직 못 만듦 — featurizer(sqlglot) 먼저 필요.
@app.command(name="rule-from")
def rule_from(
    log: str,
    event: str = typer.Option(..., "--event", help="예: evt_0042"),
    output: str = typer.Option("rules.yaml", "-o", "--output"),
):
    """실패 이벤트로부터 후보 규칙을 합성하고 회귀 검증 후 동결한다."""
    raise NotImplementedError


# TODO(가희/세림): §4 명세는 --rules를 필수로 두는데(부분 리포트 모드 없음) 시그니처에
# 아예 없음. rule-from이 없어 회귀 매트릭스 재료(blocks/regressions)도 아직 못 만든다 —
# rule-from 먼저 구현된 뒤에야 §11의 4요소(타임라인/지표/후보 회귀 표/채택 규칙 매트릭스) 착수 가능.
@app.command()
def report(
    log: str,
    output: str = typer.Option("report.html", "-o", "--output"),
):
    """분기 타임라인/지표/후보 회귀 표/규칙 회귀 매트릭스를 담은 정적 HTML을 만든다."""
    raise NotImplementedError


if __name__ == "__main__":
    app()
