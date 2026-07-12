"""가드레일 파이프라인 (CLAUDE.md §5). 순서 있는 순수 함수 리스트 + 첫 non-allow 승리.

이 파일은 harness.py의 Harness 생성자 계약이 의존하는 최소 계약만 담는다.
실제 4단계 스테이지 함수(schema/permission/budget/safety) 본체와 집행 엔진은
별도 구현 범위다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from rein.guardrails.verdict import Verdict

# §5 표: schema -> permission -> budget -> safety, short-circuit 순서의 "기본 정책 번들" 이름.
DEFAULT_STAGE_ORDER: tuple[str, ...] = ("schema", "permission", "budget", "safety")

# §5 충돌 해결 우선순위(deny > approve > retry > allow)는 스테이지 간이 아니라
# rules.yaml 내 다중 매칭 규칙 간에만 적용된다(CLAUDE.md §5, 이슈 #34 결정).
# SSOT는 guardrails/verdict.py의 Verdict IntEnum .value — 별도 매핑 dict를 두면
# 두 정의가 어긋날 위험만 생기므로(과거 VERDICT_PRIORITY가 실제로 Verdict와
# 다른 값을 가진 채 미사용으로 방치돼 있었다) 여기 두지 않는다. 우선순위가
# 필요한 곳(cli.py::_verdict_from_rules)은 Verdict.value를 직접 참조한다.


@runtime_checkable
class StageFn(Protocol):
    """§5: Callable[[ToolCall, Context], Verdict].

    실제 런타임 계약은 4-tuple `(verdict, rule_id, rationale, evt_id)`다.
    `Harness._intercept`(harness.py)가 이 4개 값을 그대로 unpack하고,
    `rule_id`/`rationale`/`evt_id`는 non-allow 판정을 예외로 환원할 때
    (§4 `GuardrailVerdictError`) 그대로 실려 나간다 — 반환값을 `Verdict`
    단독으로 줄이면 이 세 필드를 실어 보낼 자리가 없어진다(이슈 #33).
    """

    def __call__(self, tool_call: Any, context: Any) -> tuple[Verdict, str, str, str]: ...


class UnknownStageError(ValueError):
    """stage_order가 미등록 스테이지를 참조 (fail-closed, §5)."""


def load_stage_order(config_path: str | Path) -> list[str]:
    """config 파일(rein.yaml)에서 stage_order "이름 목록"만 읽는다.

    파일이 없으면 §4 "없으면 기본 4단계 순서"에 따라 DEFAULT_STAGE_ORDER를 쓴다.
    여기서 하는 검증은 구조(YAML 파싱, stage_order가 문자열 리스트인지)뿐이다.
    register_stage로 등록되는 커스텀 스테이지의 존재 여부는 아직 알 수 없으므로
    이름 자체의 유효성은 검증하지 않는다 — 그건 resolve_stage_order의 몫이다.
    """
    path = Path(config_path)
    if not path.exists():
        return list(DEFAULT_STAGE_ORDER)
    data = yaml.safe_load(path.read_text()) or {}
    order = data.get("stage_order", list(DEFAULT_STAGE_ORDER))
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        raise ValueError(f"{config_path}: stage_order는 문자열 리스트여야 합니다.")
    return order


def resolve_stage_order(stage_order: list[str], custom_stages: dict[str, StageFn]) -> list[str]:
    """§5 fail-closed: stage_order의 각 이름이 내장 4단계이거나 register_stage로
    등록된 커스텀 스테이지인지 검증한다. 하나라도 미등록이면 조용히 무시하지
    않고 즉시 UnknownStageError를 던진다.
    """
    known = set(DEFAULT_STAGE_ORDER) | set(custom_stages)
    unknown = [name for name in stage_order if name not in known]
    if unknown:
        raise UnknownStageError(
            f"stage_order에 미등록 스테이지: {unknown}. "
            "h.register_stage(name, fn)로 먼저 등록하거나 rein.yaml 오타를 수정하세요."
        )
    return list(stage_order)
