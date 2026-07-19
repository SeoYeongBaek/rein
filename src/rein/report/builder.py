"""실제 JSONL과 rules.yaml을 report.html용 데이터로 변환함."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

from rein import rules
from rein.events.event_store import (
    SOURCE_MODEL_CLIENT,
    SOURCE_OUTCOME,
    SOURCE_TOOL_WRAP,
)
from rein.guardrails.verdict import Verdict
from rein.report.models import (
    CandidateRegressionRow,
    CorpusType,
    RegressionLabel,
    RegressionMatrixRow,
    ReportData,
    ReportMetrics,
    RuleAnalysis,
    TimelinePhase,
    TimelineRow,
)
from rein.rules.runtime import (
    _load_rules,
    _to_verdict,
    _verdict_from_rules,
    matching_rules,
)

# rule-from이 cold-start 규칙의 provenance에 기록하는 문자열 형식임.
_COLD_START_PATTERN = re.compile(
    r"^(?P<log>.+?) "
    r"\(cold-start subset, excludes born_from=(?P<event>[^)]+)\)"
    r"(?: \+ (?P<config>.+?) permissions)?$"
)


class ReportError(ValueError):
    """리포트 입력 또는 데이터 정합성 오류."""


def build_report_data(
    log_path: Path,
    rules_path: Path,
) -> ReportData:
    """리포트 4요소에 필요한 데이터를 구성함."""
    events = _load_jsonl(log_path)

    try:
        loaded_rules = _load_rules([rules_path])
    except ValueError as exc:
        raise ReportError(str(exc)) from exc

    if not loaded_rules:
        raise ReportError(f"{rules_path}: 유효한 rule 문서가 없습니다.")

    tool_events = _tool_events(events)

    if not tool_events:
        raise ReportError(f"{log_path}: tool_wrap 이벤트가 없습니다.")

    timeline, intervention_seq = _build_timeline(
        events=events,
        tool_events=tool_events,
        loaded_rules=loaded_rules,
    )

    metrics = _build_metrics(timeline)

    rule_analyses = _build_rule_analyses(
        log_path=log_path,
        rules_path=rules_path,
        tool_events=tool_events,
        loaded_rules=loaded_rules,
    )

    if not rule_analyses:
        raise ReportError(f"{rules_path}: provenance.born_from이 있는 자동 생성 규칙이 없습니다.")

    return ReportData(
        log_path=str(log_path),
        rules_path=str(rules_path),
        intervention_seq=intervention_seq,
        timeline=tuple(timeline),
        metrics=metrics,
        rule_analyses=tuple(rule_analyses),
    )


def _load_jsonl(
    path: Path,
) -> list[dict[str, Any]]:
    """JSONL 파일을 이벤트 목록으로 읽음."""
    if not path.exists():
        raise ReportError(f"{path} 파일이 없습니다.")

    events: list[dict[str, Any]] = []

    try:
        with path.open(encoding="utf-8") as file:
            for line_number, raw_line in enumerate(
                file,
                start=1,
            ):
                line = raw_line.strip()

                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ReportError(f"{path}:{line_number} JSONL 파싱 실패: {exc}") from exc

                if not isinstance(event, dict):
                    raise ReportError(f"{path}:{line_number} 이벤트는 JSON 객체여야 합니다.")

                events.append(event)

    except OSError as exc:
        raise ReportError(f"{path} 파일을 읽을 수 없습니다: {exc}") from exc

    return events


def _tool_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """tool_wrap 이벤트만 seq 순서로 반환함."""
    result = [event for event in events if event.get("source") == SOURCE_TOOL_WRAP]

    for event in result:
        if not isinstance(event.get("seq"), int):
            raise ReportError(
                f"tool_wrap 이벤트 {event.get('evt', '<unknown>')}의 seq가 정수가 아닙니다."
            )

    return sorted(
        result,
        key=lambda event: event["seq"],
    )


def _build_timeline(
    *,
    events: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    loaded_rules: list[dict[str, Any]],
) -> tuple[list[TimelineRow], int | None]:
    """분기 타임라인과 최초 개입 seq를 생성함."""
    outcomes_by_event = {
        event.get("evt"): event.get("outcome") or {}
        for event in events
        if event.get("source") == SOURCE_OUTCOME
    }

    # model_client는 parent_seq가 같은 tool_wrap 직전에 표시함.
    model_events_by_parent: dict[
        int,
        list[dict[str, Any]],
    ] = {}

    for event in events:
        if event.get("source") != SOURCE_MODEL_CLIENT:
            continue

        parent_seq = event.get("parent_seq")

        if isinstance(parent_seq, int):
            model_events_by_parent.setdefault(
                parent_seq,
                [],
            ).append(event)

    comparisons: list[
        tuple[
            dict[str, Any],
            str,
            str,
            tuple[str, ...],
        ]
    ] = []

    intervention_seq: int | None = None

    for event in tool_events:
        try:
            off_verdict = str(_to_verdict(event.get("verdict") or "allow"))
            on_verdict = _verdict_from_rules(
                event,
                loaded_rules,
            )
        except ValueError as exc:
            raise ReportError(str(exc)) from exc

        matched_rule_ids = tuple(
            str(rule.get("id", "<unnamed>"))
            for rule in matching_rules(
                event,
                loaded_rules,
            )
        )

        comparisons.append(
            (
                event,
                off_verdict,
                on_verdict,
                matched_rule_ids,
            )
        )

        if intervention_seq is None and off_verdict != on_verdict:
            intervention_seq = event["seq"]

    timeline: list[TimelineRow] = []

    for (
        event,
        off_verdict,
        on_verdict,
        matched_rule_ids,
    ) in comparisons:
        seq = event["seq"]

        phase = _timeline_phase(
            seq=seq,
            intervention_seq=intervention_seq,
        )

        for model_event in model_events_by_parent.get(
            seq,
            [],
        ):
            model_args = model_event.get("args")

            if not isinstance(model_args, dict):
                model_args = {"tool_uses": model_event.get("tool_uses")}

            timeline.append(
                TimelineRow(
                    kind="model_client",
                    seq=None,
                    event_id=str(model_event.get("evt", "-")),
                    tool_name=str(model_event.get("tool_name") or "LLM tool_use"),
                    role=None,
                    args=model_args,
                    off_verdict=None,
                    on_verdict=None,
                    phase=phase,
                    severity=None,
                    detail="모델이 제안한 도구 호출",
                    matched_rule_ids=(),
                )
            )

        outcome = outcomes_by_event.get(
            event.get("evt"),
            {},
        )

        context = event.get("context") or {}

        timeline.append(
            TimelineRow(
                kind="tool_wrap",
                seq=seq,
                event_id=str(event.get("evt", "-")),
                tool_name=str(event.get("tool_name", "-")),
                role=context.get("agent_role"),
                args=event.get("args") or {},
                off_verdict=off_verdict,
                on_verdict=on_verdict,
                phase=phase,
                severity=outcome.get("severity"),
                detail=outcome.get("detail"),
                matched_rule_ids=matched_rule_ids,
            )
        )

    return timeline, intervention_seq


def _timeline_phase(
    *,
    seq: int,
    intervention_seq: int | None,
) -> TimelinePhase:
    """현재 이벤트의 타임라인 구간을 반환함."""
    if intervention_seq is None or seq < intervention_seq:
        return "shared"

    if seq == intervention_seq:
        return "intervention"

    return "diverged"


def _build_metrics(
    timeline: list[TimelineRow],
) -> ReportMetrics:
    """실제 tool_wrap 행을 기준으로 지표를 계산함."""
    tool_rows = [row for row in timeline if row.kind == "tool_wrap"]

    return ReportMetrics(
        total_events=len(tool_rows),
        critical_off=sum(row.severity == "critical" for row in tool_rows),
        blocked_on=sum(row.on_verdict == "deny" for row in tool_rows),
        changed_count=sum(row.off_verdict != row.on_verdict for row in tool_rows),
    )


def _build_rule_analyses(
    *,
    log_path: Path,
    rules_path: Path,
    tool_events: list[dict[str, Any]],
    loaded_rules: list[dict[str, Any]],
) -> list[RuleAnalysis]:
    """후보 회귀 표와 채택 규칙 매트릭스를 생성함."""
    events_by_id = {str(event.get("evt")): event for event in tool_events}

    analyses: list[RuleAnalysis] = []

    for adopted_rule in loaded_rules:
        provenance = adopted_rule.get("provenance") or {}

        born_from_id = provenance.get("born_from")

        # 수동 규칙처럼 provenance가 없는 규칙은 후보 분석에서 제외함.
        if not isinstance(born_from_id, str):
            continue

        born_from = events_by_id.get(born_from_id)

        if born_from is None:
            raise ReportError(
                f"규칙 "
                f"{adopted_rule.get('id', '<unnamed>')}: "
                f"born_from={born_from_id}를 "
                f"{log_path}에서 찾을 수 없습니다."
            )

        validated_against = str(provenance.get("validated_against") or "")

        negatives = _load_negatives(
            validated_against=validated_against,
            log_path=log_path,
            rules_path=rules_path,
            current_tool_events=tool_events,
            born_from=born_from,
        )

        try:
            synthesis = rules.synthesize_rule(
                born_from,
                negatives,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReportError(
                f"규칙 {adopted_rule.get('id', '<unnamed>')}의 후보 회귀 계산 실패: {exc}"
            ) from exc

        stored_rank = str(provenance.get("generality_rank") or "")

        recomputed_rank = str(synthesis["generality_rank"])

        selected_rank = stored_rank or recomputed_rank

        candidate_rows = tuple(
            CandidateRegressionRow(
                depth=int(entry["depth"]),
                when=entry["when"],
                scope=entry["scope"],
                regression_ids=tuple(str(event_id) for event_id in entry["regressions"]),
                selected=(f"{entry['depth']}/3" == selected_rank),
            )
            for entry in synthesis["candidate_trail"]
        )

        feature_schema = provenance.get("feature_schema")

        feature_schema_stale = bool(
            feature_schema and feature_schema != rules.FEATURE_SCHEMA_VERSION
        )

        analyses.append(
            RuleAnalysis(
                rule_id=str(
                    adopted_rule.get(
                        "id",
                        "<unnamed>",
                    )
                ),
                rationale=str(adopted_rule.get("rationale") or "-"),
                adopted_rule=adopted_rule,
                born_from=born_from_id,
                validated_against=(validated_against or "-"),
                provenance_blocks=tuple(str(value) for value in provenance.get("blocks") or []),
                provenance_regressions=tuple(
                    str(value) for value in provenance.get("regressions") or []
                ),
                generality_rank=selected_rank,
                candidate_rows=candidate_rows,
                matrix_rows=_build_matrix_rows(
                    adopted_rule=adopted_rule,
                    born_from=born_from,
                    negatives=negatives,
                ),
                stale=(
                    feature_schema_stale or bool(stored_rank and stored_rank != recomputed_rank)
                ),
            )
        )

    return analyses


def _load_negatives(
    *,
    validated_against: str,
    log_path: Path,
    rules_path: Path,
    current_tool_events: list[dict[str, Any]],
    born_from: dict[str, Any],
) -> list[dict[str, Any]]:
    """provenance가 가리키는 정상 코퍼스를 로드함."""
    if not validated_against:
        return []

    cold_start_match = _COLD_START_PATTERN.fullmatch(validated_against)

    if cold_start_match:
        recorded_event = cold_start_match.group("event")

        if recorded_event != str(born_from.get("evt")):
            raise ReportError(
                "validated_against의 born_from과 규칙 provenance.born_from이 다릅니다."
            )

        negatives = _cold_start_negatives(
            events=current_tool_events,
            born_from=born_from,
        )

        config_text = cold_start_match.group("config")

        if config_text:
            config_path = _resolve_existing_path(
                raw_path=config_text,
                log_path=log_path,
                rules_path=rules_path,
            )

            try:
                permission_table = rules.load_permission_table(config_path)
                negatives.extend(
                    rules.permission_table_negatives(
                        born_from,
                        permission_table,
                    )
                )
            except ValueError as exc:
                raise ReportError(str(exc)) from exc

        return negatives

    corpus_path = _resolve_existing_path(
        raw_path=validated_against,
        log_path=log_path,
        rules_path=rules_path,
    )

    # 이전 형식에서 현재 run.jsonl 경로만 저장한 경우도 지원함.
    if corpus_path.resolve() == log_path.resolve():
        return _cold_start_negatives(
            events=current_tool_events,
            born_from=born_from,
        )

    return _tool_events(_load_jsonl(corpus_path))


def _resolve_existing_path(
    *,
    raw_path: str,
    log_path: Path,
    rules_path: Path,
) -> Path:
    """상대 경로를 현재 위치, rules 위치, log 위치에서 탐색함."""
    path = Path(raw_path)

    candidates = [path]

    if not path.is_absolute():
        candidates.extend(
            [
                rules_path.parent / path,
                log_path.parent / path,
            ]
        )

    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise ReportError(
        "검증 코퍼스 또는 설정 파일을 찾을 수 없습니다: "
        + ", ".join(str(candidate) for candidate in candidates)
    )


def _cold_start_negatives(
    *,
    events: list[dict[str, Any]],
    born_from: dict[str, Any],
) -> list[dict[str, Any]]:
    """현재 로그에서 안전한 정상 호출만 음성으로 선택함."""
    tool_name = born_from.get("tool_name")
    born_event_id = born_from.get("evt")

    negatives: list[dict[str, Any]] = []

    for event in events:
        features = rules.featurize(event.get("args") or {})

        event_class = features.get("class") if features else None

        severity = rules.SEVERITY_TABLE.get(event_class) if event_class else None

        if (
            event.get("tool_name") == tool_name
            and event.get("verdict") == "allow"
            and event.get("evt") != born_event_id
            and severity == "info"
        ):
            negatives.append(event)

    return negatives


def _build_matrix_rows(
    *,
    adopted_rule: dict[str, Any],
    born_from: dict[str, Any],
    negatives: list[dict[str, Any]],
) -> tuple[RegressionMatrixRow, ...]:
    """채택 규칙을 양성 1건과 음성 전체에 적용함."""
    corpus: list[
        tuple[
            CorpusType,
            dict[str, Any],
        ]
    ] = [
        ("positive", born_from),
        *[("negative", event) for event in negatives],
    ]

    rows: list[RegressionMatrixRow] = []

    for corpus_type, event in corpus:
        matched = rules.rule_matches(
            adopted_rule,
            event,
        )

        applied_verdict = (
            str(_to_verdict(adopted_rule.get("then", "deny"))) if matched else str(Verdict.ALLOW)
        )

        label: RegressionLabel

        if corpus_type == "positive":
            label = "Blocked" if applied_verdict == "deny" else "Missed"
            is_regression = False
        else:
            label = "Pass" if applied_verdict == "allow" else "Blocked"
            is_regression = applied_verdict == "deny"

        context = event.get("context") or {}

        rows.append(
            RegressionMatrixRow(
                corpus_type=cast(
                    CorpusType,
                    corpus_type,
                ),
                event_id=str(event.get("evt", "-")),
                action=_action_summary(event),
                tool_name=str(
                    event.get(
                        "tool_name",
                        "-",
                    )
                ),
                role=context.get("agent_role"),
                applied_verdict=applied_verdict,
                label=cast(
                    RegressionLabel,
                    label,
                ),
                is_regression=is_regression,
            )
        )

    return tuple(rows)


def _action_summary(
    event: dict[str, Any],
) -> str:
    """이벤트 인자를 표에 표시할 문자열로 변환함."""
    args = event.get("args") or {}

    for key in (
        "query",
        "command",
        "path",
    ):
        value = args.get(key)

        if isinstance(value, str):
            return value

    return json.dumps(
        args,
        ensure_ascii=False,
        sort_keys=True,
    )
