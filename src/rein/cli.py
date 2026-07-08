"""rein CLI. CLAUDE.md §4: 공개 인터페이스로 취급, M1에서 확정.

명령어별 담당:
- seed:      골든 트레이스 녹화
- replay:    record/replay-verify/live-rerun 3모드
- rule-from: 실패 이벤트 → 규칙 생성
- report:    정적 report.html 렌더
"""

import hashlib
import importlib.metadata
import re
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from rein import rules
from rein.replay.engine import ReplayMismatchError, _load_tool_wrap_events

app = typer.Typer(name="rein", help="Agent = Model + Harness")
console = Console()

_RULE_ID_RE = re.compile(r"^rule_(\d+)$")


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


def _find_event(events: list[dict[str, Any]], evt_id: str) -> dict[str, Any] | None:
    return next((e for e in events if e.get("evt") == evt_id), None)


def _cold_start_negatives(
    events: list[dict[str, Any]], born_from: dict[str, Any]
) -> list[dict[str, Any]]:
    """--golden 미지정 시 합성 음성(§7 안전장치 ②, 이슈 #11 확정) 도출.

    run_log 자신의 tool_wrap 이벤트 중 tool_name 동일 + verdict=="allow" +
    outcome.severity=="info" + evt != born_from.evt 인 것만 음성으로 삼는다
    (전제 조건은 rules.__init__ 모듈 docstring 참고 — 이 severity는 로그에
    이미 기록된 값을 그대로 읽으며, featurize()로 재계산하지 않는다).
    """
    tool_name = born_from.get("tool_name")
    born_evt = born_from.get("evt")
    return [
        e
        for e in events
        if e.get("tool_name") == tool_name
        and e.get("verdict") == "allow"
        and (e.get("outcome") or {}).get("severity") == "info"
        and e.get("evt") != born_evt
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
      - outcome.severity == "info" (class 기반 산출값이라 이 필터
        하나로 §8 "validated_against는 음성 전용" 요구가 충족된다.
        다른 role의 DROP TABLE 같은 위험 호출이 섞여 들어올 수 없다)
      - evt != born_from.evt
      - agent.role은 필터 조건 아님 (같은 role의 무해한 호출, 다른
        role의 같은 도구 호출 둘 다 유효한 음성)
    후보가 0건이면 일반화하지 않고 빔서치 깊이 3(tool_name+class+
    agent.role 모두 born_from 값 고정)인 가장 좁은 후보만 채택한다.
    실제 게이팅 구현은 이슈 #10(synthesize & verify) 쪽에서 한다.

    전제 조건(§7 featurize 의존): 위 severity == "info" 필터는 로그에 이미
    기록된 outcome.severity가 §7 분류 테이블을 통해 featurizer가 결정론적으로
    계산한 값이라는 전제 위에서만 안전하다. 그 전제가 깨진 로그(예: severity가
    수기로 채워졌거나 다른 버전의 분류 테이블로 계산된 경우)에서는 "info ==
    무해"라는 가정이 깨져 위험 호출이 합성 음성에 섞여 들어올 수 있다.
    rules 모듈은 이 리스크를 피하려 evt.args를 featurize()로 다시 계산해서
    class를 매칭하지만, 이 cold-start 음성 "선정" 자체는 로그의 severity
    필드를 그대로 신뢰한다.

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
    rules: Annotated[
        str, typer.Option("--rules", help="필수 — 후보/채택 규칙 회귀 매트릭스 렌더용")
    ],
    output: Annotated[str, typer.Option("-o", "--output")] = "report.html",
):
    """분기 타임라인/지표/후보 회귀 표/규칙 회귀 매트릭스를 담은 정적 HTML을 만든다."""
    raise NotImplementedError


if __name__ == "__main__":
    app()
