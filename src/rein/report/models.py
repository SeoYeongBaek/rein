"""정적 report.html에 전달할 데이터 모델."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

TimelineKind = Literal[
    "model_client",
    "tool_wrap",
]

TimelinePhase = Literal[
    "shared",
    "intervention",
    "diverged",
]

CorpusType = Literal[
    "positive",
    "negative",
]

RegressionLabel = Literal[
    "Blocked",
    "Pass",
    "Missed",
]


@dataclass(frozen=True)
class TimelineRow:
    """분기 타임라인의 한 행."""

    kind: TimelineKind
    seq: int | None
    event_id: str
    tool_name: str
    role: str | None
    args: dict[str, Any]
    off_verdict: str | None
    on_verdict: str | None
    phase: TimelinePhase
    severity: str | None
    detail: str | None
    matched_rule_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReportMetrics:
    """Before/After 요약 지표."""

    total_events: int
    critical_off: int
    blocked_on: int
    changed_count: int


@dataclass(frozen=True)
class CandidateRegressionRow:
    """후보 규칙 회귀 표의 한 행."""

    depth: int
    when: dict[str, Any]
    scope: dict[str, Any] | None
    regression_ids: tuple[str, ...]
    selected: bool


@dataclass(frozen=True)
class RegressionMatrixRow:
    """채택 규칙 회귀 매트릭스의 한 행."""

    corpus_type: CorpusType
    event_id: str
    action: str
    tool_name: str
    role: str | None
    applied_verdict: str
    label: RegressionLabel
    is_regression: bool


@dataclass(frozen=True)
class RuleAnalysis:
    """규칙 하나의 후보 및 회귀 분석 결과."""

    rule_id: str
    rationale: str
    adopted_rule: dict[str, Any]
    born_from: str
    validated_against: str
    provenance_blocks: tuple[str, ...]
    provenance_regressions: tuple[str, ...]
    generality_rank: str
    candidate_rows: tuple[CandidateRegressionRow, ...]
    matrix_rows: tuple[RegressionMatrixRow, ...]
    stale: bool


@dataclass(frozen=True)
class ReportData:
    """HTML 템플릿에 전달할 최종 데이터."""

    log_path: str
    rules_path: str
    intervention_seq: int | None
    timeline: tuple[TimelineRow, ...]
    metrics: ReportMetrics
    rule_analyses: tuple[RuleAnalysis, ...]
