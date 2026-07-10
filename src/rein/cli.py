"""rein CLI. CLAUDE.md §4: 공개 인터페이스로 취급, M1에서 확정.

명령어별 담당:
- seed:      골든 트레이스 녹화
- replay:    record/replay-verify/live-rerun 3모드
- rule-from: 실패 이벤트 → 규칙 생성
- report:    정적 report.html 렌더
"""

import warnings
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
            if doc is None:
                continue  # `---`가 만드는 빈 문서 — 정상 케이스, 조용히 스킵
            if "rule" not in doc:
                warnings.warn(
                    f"{path}: 'rule' 키가 없는 YAML 문서를 건너뜁니다 "
                    f"(최상위 키: {sorted(doc.keys())})",
                    stacklevel=2,
                )
                continue
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
    for verdict in matched:
        if verdict not in _VERDICT_PRIORITY:
            raise ValueError(
                f"규칙의 then 값이 잘못되었습니다: {verdict!r} "
                f"(허용값: {sorted(_VERDICT_PRIORITY)})"
            )
    return max(matched, key=lambda v: _VERDICT_PRIORITY[v])


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
    log_path = Path(log)
    if not log_path.exists():
        typer.echo(f"오류: {log} 파일이 없습니다.", err=True)
        raise typer.Exit(1)

    try:
        engine = ReplayEngine(log_path, mode="replay-verify")
    except ReplayMismatchError as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(1) from None

    events = list(engine)
    if not events:
        typer.echo("tool_wrap 이벤트가 없습니다.")
        return

    if compare:
        try:
            _print_compare(events, rules or [])
        except ValueError as e:
            typer.echo(f"오류: {e}", err=True)
            raise typer.Exit(1) from None
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
        # 스타일이 없는(미지정) severity에 빈 태그 "[]...[/]" 를 씌우면 rich가
        # MarkupError를 던진다 — 스타일이 있을 때만 태그로 감싼다.
        severity_text = (
            f"[{severity_style}]{severity}[/{severity_style}]" if severity_style else severity
        )
        table.add_row(
            str(evt.get("seq", "-")),
            evt.get("tool_name", "-"),
            evt.get("verdict", "-"),
            severity_text,
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
    typer.echo(
        f"report 옵션 파싱 완료: log={log}, rules={rules}, output={output} "
        "(M1에서는 렌더링하지 않음)"
    )


if __name__ == "__main__":
    app()
