"""rein CLI. CLAUDE.md §4: 공개 인터페이스로 취급, M1에서 확정.

명령어별 담당:
- seed:      골든 트레이스 검증
- replay:    record/replay-verify/live-rerun 3모드
- rule-from: 실패 이벤트 → 규칙 생성
- report:    정적 report.html 렌더
"""

import hashlib
import importlib.metadata
import json
import re
import warnings
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from rein import rules
from rein.events.event_store import SCHEMA_VERSION, SEVERITY_CRITICAL, SOURCE_OUTCOME
from rein.guardrails.verdict import Verdict
from rein.replay.engine import ReplayEngine, ReplayMismatchError, _load_tool_wrap_events

app = typer.Typer(name="rein", help="Agent = Model + Harness")
console = Console()

_RULE_ID_RE = re.compile(r"^rule_(\d+)$")

# ---- rein seed: 검증 헬퍼 ----


class SeedValidationError(Exception):
    """§4 seed 검증 실패. fail-closed — 조용한 통과 금지."""


# §9 이벤트 스키마 검증 — rein seed가 통과시켜야 하는 최소 필드 셋.
# §9 표에 명시된 필드만 검사. 모르는 필드는 무시(forward-compat).
_REQUIRED_TOOL_WRAP_FIELDS = {
    "schema_version",
    "evt",
    "seq",
    "source",
    "parent_seq",
    "tool_name",
    "args",
    "context",
    "verdict",
}
_REQUIRED_OUTCOME_FIELDS = {
    "schema_version",
    "evt",
    "seq",
    "source",
    "parent_seq",
    "tool_name",
    "outcome",
}
_OUTCOME_REQUIRED_SUBFIELDS = {"status", "severity", "detail"}


def _validate_run_log(run_log: Path) -> int:
    """run.jsonl을 줄 단위로 검증. tool_wrap 이벤트가 1개 이상이어야 한다.

    Raises:
        SeedValidationError: 스키마 위반 또는 §4 critical outcome 검출 시.
    """
    if not run_log.exists():
        raise SeedValidationError(f"{run_log} 파일이 없습니다.")

    line_count = 0
    tool_wrap_count = 0
    with run_log.open(encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            line_count += 1

            try:
                evt = json.loads(stripped)
            except json.JSONDecodeError as e:
                raise SeedValidationError(f"{run_log}:{line_no} JSONL 파싱 실패: {e}") from e

            source = evt.get("source")
            if source == "tool_wrap":
                _check_schema(evt, line_no, run_log, _REQUIRED_TOOL_WRAP_FIELDS)
                tool_wrap_count += 1
            elif source == SOURCE_OUTCOME:
                _check_schema(evt, line_no, run_log, _REQUIRED_OUTCOME_FIELDS)
                _check_outcome(evt, line_no, run_log)
            elif source == "model_client":
                # §9 model_client는 seq=null — 검증 스킵(스키마 표 외)
                continue
            else:
                raise SeedValidationError(f"{run_log}:{line_no} 알 수 없는 source={source!r}")

    if tool_wrap_count == 0:
        raise SeedValidationError(
            f"{run_log}: tool_wrap 이벤트가 0건입니다 — 골든 시드로 지정 불가"
        )

    return line_count


def _check_schema(evt: dict[str, Any], line_no: int, run_log: Path, required: set[str]) -> None:
    missing = required - set(evt.keys())
    if missing:
        raise SeedValidationError(f"{run_log}:{line_no} 필수 필드 누락: {sorted(missing)}")
    if evt.get("schema_version") != SCHEMA_VERSION:
        raise SeedValidationError(
            f"{run_log}:{line_no} schema_version={evt.get('schema_version')!r} "
            f"(기대값: {SCHEMA_VERSION!r})"
        )


def _check_outcome(evt: dict[str, Any], line_no: int, run_log: Path) -> None:
    outcome = evt.get("outcome") or {}
    missing = _OUTCOME_REQUIRED_SUBFIELDS - set(outcome.keys())
    if missing:
        raise SeedValidationError(f"{run_log}:{line_no} outcome 필수 필드 누락: {sorted(missing)}")
    # §4 "critical outcome 0건 확인"
    if outcome.get("severity") == SEVERITY_CRITICAL:
        raise SeedValidationError(
            f"{run_log}:{line_no} critical outcome 검출 — 골든 트레이스에 부적합 "
            f"(evt={evt.get('evt')}, detail={outcome.get('detail')!r})"
        )


# ---- §5 충돌 해결 ----
# §5: deny > approve > retry > allow. 우선순위는 verdict.py의 IntEnum 값 자체가
# 정수이므로(ALLOW=1, RETRY=2, APPROVE=3, DENY=4) 별도 매핑 dict를 두지 않고
# .value로 직접 비교한다 — verdict.py가 SSOT, cli.py는 따라갈 뿐.
#
# §5 단일 정수 비교:
#   DENY=4, APPROVE=3, RETRY=2, ALLOW=1 → max(.value)로 가장 제한적인 판정 선택.


def _load_rules(rules_paths: list[str]) -> list[dict]:
    """rules.yaml 여러 개를 평탄화. 파일 하나에 `---`로 구분된 여러 규칙 문서도 지원
    (§4 rule-from의 append 동작을 받아내려면 한 파일에 규칙이 여러 개 쌓일 수 있음).
    """
    loaded = []
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
            loaded.append(doc["rule"])
    return loaded


def _rule_matches(rule: dict, evt: dict) -> bool:
    """when.tool + scope.agent.role 매칭.

    이슈 #10에서 구현한 rein.rules.rule_matches와 달리 when.features.class는
    보지 않는다 — replay --compare(이슈 #9)는 rein.rules에 의존하지 않고
    독립적으로 구현됐다. 두 매칭기가 갈라져 있는 상태는 알려진 갭이며,
    통합은 별도 이슈로 다룬다.
    """
    when = rule.get("when", {})
    if when.get("tool") and when.get("tool") != evt.get("tool_name"):
        return False

    scope = rule.get("scope") or {}
    scoped_role = scope.get("agent.role")
    if scoped_role and scoped_role != evt.get("context", {}).get("agent_role"):
        return False

    return True


def _to_verdict(value: str) -> Verdict:
    """문자열 → Verdict 변환 헬퍼. name과 value(정수) 둘 다 허용.

    IntEnum은 기본적으로 value(정수) 매칭만 허용하지만, rein은 rules.yaml의
    then: deny 같은 문자열을 받아야 하므로 name 매칭도 지원한다.
    잘못된 값은 ValueError로 환원 — §5 fail-closed (조용한 allow 취급 금지).
    """
    # §5 fail-closed: then: null / then: 1 같은 비-str 입력은 str 검증 이전에
    # 친절한 메시지로 거절한다. None이면 .upper() 호출에서 AttributeError가
    # 새어나가 사용자 스택트레이스를 노출시키므로 (fail-closed 위반) 여기서 차단.
    if not isinstance(value, str):
        raise ValueError(
            f"허용되지 않은 verdict 타입: {type(value).__name__}={value!r} "
            f"(허용값: {[v.name.lower() for v in Verdict]})"
        )
    try:
        return Verdict(value)  # value(정수) 매칭
    except ValueError:
        try:
            return Verdict[value.upper()]  # name 매칭 (대소문자 무시)
        except (KeyError, AttributeError) as e:
            # AttributeError는 위 isinstance 가드가 정상 흐름에서 막지만,
            # str이 아닌 객체의 .upper()가 우연히 정의돼 있는 경우 등
            # 의외 경로를 위한 마지막 방어선.
            raise ValueError(
                f"허용되지 않은 verdict: {value!r} (허용값: {[v.name.lower() for v in Verdict]})"
            ) from e


def _verdict_from_rules(evt: dict, loaded_rules: list[dict]) -> str:
    """규칙 적용 후 판정. 매칭된 규칙이 여럿이면 §5 충돌 해결 우선순위
    (deny > approve > retry > allow)로 가장 제한적인 판정을 고른다.

    loaded_rules는 미리 _load_rules로 로드된 규칙 리스트 — 이벤트마다 파일을
    다시 읽지 않도록 호출자(_print_compare)가 루프 밖에서 한 번만 로드해서 넘긴다.

    우선순위는 verdict.py의 IntEnum .value(SSOT)에서 직접 도출:
    DENY=4 > APPROVE=3 > RETRY=2 > ALLOW=1. 별도 매핑 dict 없음.

    TODO: §5 가드레일 4단계(schema/permission/budget/safety) 자체는 아직 없어서
    여기선 rules.yaml 매칭만 수행 — 가드레일 엔진 연결 후 교체.
    """
    matched = [rule.get("then", "allow") for rule in loaded_rules if _rule_matches(rule, evt)]
    if not matched:
        return str(Verdict.ALLOW)  # = "allow"
    try:
        verdicts = [_to_verdict(v) for v in matched]
    except ValueError as e:
        raise ValueError(
            f"규칙의 then 값이 잘못되었습니다: {matched!r} "
            f"(허용값: {[v.name.lower() for v in Verdict]})"
        ) from e
    # §5: max(.value) — 가장 제한적인 판정. Verdict.DENY.value(=4)가 승리.
    return str(max(verdicts, key=lambda v: v.value))


@app.command()
def seed(
    run_log: Annotated[
        str, typer.Argument(help="검증할 run.jsonl 경로 (Harness(record=...)로 녹화됨)")
    ],
) -> None:
    """스키마 + critical outcome 0건을 검증한 뒤 golden_run.jsonl로 지정한다 (§4).

    러너가 아니다 — 이미 Harness(record=...)로 녹화된 JSONL을 검증만 한다.
    스크립트를 대신 실행해주는 일은 하지 않는다.
    """
    run_log_path = Path(run_log)
    # §4: 골든 트레이스 = 입력 run.jsonl 그 자체 (별도 복사 없음).
    # 메시지에서 golden_path로 참조해 변수 의미를 살리고, linter의
    # unused 경고를 피한다 — run_log_path와 다른 의도(이 경로 = 골든).
    golden_path = run_log_path

    try:
        line_count = _validate_run_log(run_log_path)
    except SeedValidationError as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(1) from None

    typer.echo(f"검증 통과: {line_count}개 라인, tool_wrap 이벤트 critical 0건")
    typer.echo(
        f"골든 트레이스로 지정됨: {golden_path} (별도 복사 없음, 이 경로를 --golden에 그대로 사용)"
    )


@app.command()
def replay(
    log: str,
    rules_paths: Annotated[
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
            _print_compare(events, rules_paths or [])
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

    loaded_rules = _load_rules(rules_paths)  # 루프 밖에서 한 번만 로드 (이벤트마다 재파싱하지 않음)
    changed_count = 0
    for evt in events:
        recorded = evt.get("verdict", "allow")
        with_rules = _verdict_from_rules(evt, loaded_rules)
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


def _find_event(events: list[dict[str, Any]], evt_id: str) -> dict[str, Any] | None:
    return next((e for e in events if e.get("evt") == evt_id), None)


def _recomputed_severity(evt: dict[str, Any]) -> str | None:
    """rules.__init__ 모듈 docstring의 featurizer guard: 로그의 outcome.severity를
    신뢰하지 않고 evt.args를 featurize()로 다시 계산해서 SEVERITY_TABLE로 severity를
    도출한다. featurize가 실패하면(비-SQL, 파싱 실패) None — "검증 불가"이지
    "info"가 아니므로 콜드 스타트 음성 후보에서 자동 제외된다.
    """
    features = rules.featurize(evt.get("args") or {})
    if features is None:
        return None
    return rules.SEVERITY_TABLE.get(features["class"])


def _cold_start_negatives(
    events: list[dict[str, Any]], born_from: dict[str, Any]
) -> list[dict[str, Any]]:
    """--golden 미지정 시 합성 음성(§7 안전장치 ②, 이슈 #11 확정) 도출.

    run_log 자신의 tool_wrap 이벤트 중 tool_name 동일 + verdict=="allow" +
    evt != born_from.evt + (featurize로 재계산한 severity)=="info" 인 것만
    음성으로 삼는다. 로그에 이미 기록된 outcome.severity 필드는 신뢰하지 않는다 —
    그 값이 §7 SEVERITY_TABLE이 아닌 다른 경로(수기 태깅 등)로 채워졌다면
    "info == 무해" 가정이 깨질 수 있기 때문이다(이슈 #10 guard, rules.__init__
    모듈 docstring 참고).
    """
    tool_name = born_from.get("tool_name")
    born_evt = born_from.get("evt")
    return [
        e
        for e in events
        if e.get("tool_name") == tool_name
        and e.get("verdict") == "allow"
        and e.get("evt") != born_evt
        and _recomputed_severity(e) == "info"
    ]


def _next_rule_id(output_path: Path) -> str:
    """-o 파일에 이미 있는 rule_(\\d+) 중 최대값+1. 없으면 rule_0001."""
    max_n = 0
    if output_path.exists():
        for doc in yaml.safe_load_all(output_path.read_text(encoding="utf-8")):
            if not doc or "rule" not in doc:
                continue
            m = _RULE_ID_RE.match(str(doc["rule"].get("id", "")))
            if m:
                max_n = max(max_n, int(m.group(1)))
    return f"rule_{max_n + 1:04d}"


def _tool_sig(tool_name: str, args: dict[str, Any]) -> str:
    """§8 provenance.tool_sig: 도구명 + 정렬된 arg 키 집합의 해시."""
    key_sig = ",".join(sorted(args.keys()))
    digest = hashlib.sha256(f"{tool_name}:{key_sig}".encode()).hexdigest()[:6]
    return f"{tool_name}:{digest}"


def _rationale(tool_name: str, klass: str | None, role: str | None, scoped: bool) -> str:
    """§8 예시("OWASP LLM06 Excessive Agency: content_editor는 파괴적 DDL 권한 없음")
    형태의 사유 문자열을 조립한다. klass가 None이면(비-SQL, featurizer 미지원)
    OWASP 태그 없이 도구명만으로 일반적인 사유를 만든다.

    em dash(—)는 쓰지 않는다: 일부 콘솔(예: 한국어 Windows 기본 cp949)은
    이 문자를 인코딩하지 못해 rich Table 출력이 UnicodeEncodeError로
    죽는다.
    """
    if klass is None:
        return f"{tool_name} 호출 차단: featurizer 미지원 도구라 세부 분류 없이 판단"

    owasp = rules.OWASP_TAGS.get(klass, "")
    prefix = f"{owasp}: " if owasp else ""
    if scoped and role is not None:
        return f"{prefix}{role}는 {tool_name}의 {klass} 권한 없음"
    return f"{prefix}{tool_name}의 {klass} 호출은 차단 대상"


def _print_dry_run(rule_doc: dict[str, Any], negatives: list[dict[str, Any]]) -> None:
    rule_body = rule_doc["rule"]

    summary = Table(title="후보 규칙", show_lines=False)
    summary.add_column("필드")
    summary.add_column("값")
    for key in ("id", "origin", "when", "scope", "then", "rationale"):
        summary.add_row(key, str(rule_body.get(key)))
    for key, value in rule_body["provenance"].items():
        summary.add_row(f"provenance.{key}", str(value))
    console.print(summary)

    matrix = Table(title="회귀 매트릭스 (음성 코퍼스)", show_lines=False)
    matrix.add_column("evt")
    matrix.add_column("tool_name")
    matrix.add_column("적용 시 판정")
    when_scope_rule = {"when": rule_body["when"], "scope": rule_body.get("scope")}
    for neg in negatives:
        would_deny = rules.rule_matches(when_scope_rule, neg)
        verdict_text = "[red]deny (회귀)[/red]" if would_deny else "allow"
        matrix.add_row(str(neg.get("evt", "-")), str(neg.get("tool_name", "-")), verdict_text)
    console.print(matrix)


def _append_rule_doc(output_path: Path, rule_doc: dict[str, Any]) -> None:
    """기존 rules.yaml이 있으면 덮어쓰지 않고 `---` 멀티 문서로 append한다."""
    yaml_text = yaml.safe_dump(rule_doc, allow_unicode=True, sort_keys=False)
    if output_path.exists() and output_path.read_text(encoding="utf-8").strip():
        with output_path.open("a", encoding="utf-8") as f:
            f.write("---\n" + yaml_text)
    else:
        output_path.write_text(yaml_text, encoding="utf-8")


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
      - (featurize로 재계산한) severity == "info" (class 기반 산출값이라 이
        필터 하나로 §8 "validated_against는 음성 전용" 요구가 충족된다.
        다른 role의 DROP TABLE 같은 위험 호출이 섞여 들어올 수 없다)
      - evt != born_from.evt
      - agent.role은 필터 조건 아님 (같은 role의 무해한 호출, 다른
        role의 같은 도구 호출 둘 다 유효한 음성)
    후보가 0건이면 일반화하지 않고 빔서치 깊이 3(tool_name+class+
    agent.role 모두 born_from 값 고정)인 가장 좁은 후보만 채택한다.
    실제 게이팅 구현은 이슈 #10(synthesize & verify) 쪽에서 한다.

    featurizer guard(이슈 #10): severity == "info" 필터는 로그에 이미 기록된
    outcome.severity 문자열을 신뢰하지 않는다. 그 값이 §7 분류 테이블이 아닌
    다른 경로(수기 태깅 등)로 채워졌다면 "info == 무해"라는 가정이 깨져 위험
    호출이 합성 음성에 섞여 들어올 수 있기 때문이다. 대신 `_recomputed_severity()`
    가 evt.args를 featurize()로 다시 계산해서 §7 SEVERITY_TABLE로 severity를
    도출하고, 그 결과가 "info"인 이벤트만 음성 후보로 삼는다. featurize가
    실패하는(비-SQL) 이벤트는 검증 불가로 간주해 자동 제외된다.

    golden-vs-synthetic 비대칭: --golden을 주면 그 파일의 tool_wrap 이벤트
    전체가 음성 코퍼스가 된다. tool_name이나 severity로 미리 걸러내지
    않는다(`rein seed`가 이미 critical outcome 0건을 보장했다는 전제이므로
    severity 필터가 필요 없고, 다른 도구 호출이 섞여도 rule_matches의
    when.tool 비교가 자연히 걸러낸다). agent.role 역시 두 경로 모두 음성
    "선정" 단계에서는 필터 조건이 아니다. role은 오직 synthesize_rule의
    depth=3 scope.agent.role 매칭에서만 개입한다.
    """
    log_path = Path(log)
    if not log_path.exists():
        typer.echo(f"오류: {log} 파일이 없습니다.", err=True)
        raise typer.Exit(1)

    try:
        events = _load_tool_wrap_events(log_path)
    except ReplayMismatchError as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(1) from None

    born_from = _find_event(events, event)
    if born_from is None:
        typer.echo(f"오류: {event} 이벤트를 {log}에서 찾을 수 없습니다.", err=True)
        raise typer.Exit(1)

    if golden is not None:
        golden_path = Path(golden)
        if not golden_path.exists():
            typer.echo(f"오류: {golden} 파일이 없습니다.", err=True)
            raise typer.Exit(1)
        negatives = _load_tool_wrap_events(golden_path)
        validated_against = golden
    else:
        negatives = _cold_start_negatives(events, born_from)
        validated_against = log

    candidate = rules.synthesize_rule(born_from, negatives)

    tool_name = born_from["tool_name"]
    role = (born_from.get("context") or {}).get("agent_role")
    features = rules.featurize(born_from.get("args") or {})
    klass = features.get("class") if features else None
    scoped = candidate["scope"] is not None

    rule_body: dict[str, Any] = {
        "id": _next_rule_id(Path(output)),
        "origin": "auto",
        "when": candidate["when"],
    }
    if candidate["scope"]:
        rule_body["scope"] = candidate["scope"]
    rule_body["then"] = candidate["then"]
    rule_body["rationale"] = _rationale(tool_name, klass, role, scoped)
    rule_body["provenance"] = {
        "born_from": born_from["evt"],
        "validated_against": validated_against,
        "blocks": candidate["blocks"],
        "regressions": candidate["regressions"],
        "generality_rank": candidate["generality_rank"],
        "extractor": f"sqlglot=={importlib.metadata.version('sqlglot')}",
        "tool_sig": _tool_sig(tool_name, born_from.get("args") or {}),
        "feature_schema": rules.FEATURE_SCHEMA_VERSION,
    }
    rule_doc = {"rule": rule_body}

    if dry_run:
        _print_dry_run(rule_doc, negatives)
        return

    _append_rule_doc(Path(output), rule_doc)
    typer.echo(f"{rule_body['id']}을(를) {output}에 기록했습니다 (born_from={event}).")


@app.command()
def report(
    log: str,
    rules_path: Annotated[
        str, typer.Option("--rules", help="필수 — 후보/채택 규칙 회귀 매트릭스 렌더용")
    ],
    output: Annotated[str, typer.Option("-o", "--output")] = "report.html",
):
    """분기 타임라인/지표/후보 회귀 표/규칙 회귀 매트릭스를 담은 정적 HTML을 만든다."""
    typer.echo(
        f"report 옵션 파싱 완료: log={log}, rules={rules_path}, output={output} "
        "(M1에서는 렌더링하지 않음)"
    )


if __name__ == "__main__":
    app()
