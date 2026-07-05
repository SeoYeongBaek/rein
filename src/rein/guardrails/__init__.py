"""가드레일 파이프라인 (CLAUDE.md §5). 순서 있는 순수 함수 리스트 + 첫 non-allow 승리.

이 파일은 harness.py의 Harness 생성자 계약이 의존하는 최소 계약만 담는다.
실제 4단계 스테이지 함수(schema/permission/budget/safety) 본체와 집행 엔진은
별도 구현 범위다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

# §5 표: schema -> permission -> budget -> safety, short-circuit 순서의 "기본 정책 번들" 이름.
DEFAULT_STAGE_ORDER: tuple[str, ...] = ("schema", "permission", "budget", "safety")

# §5 충돌 해결 우선순위: deny > approve > retry > allow (가장 제한적인 판정이 이긴다).
VERDICT_PRIORITY: dict[str, int] = {"deny": 3, "approve": 2, "retry": 1, "allow": 0}


@runtime_checkable
class StageFn(Protocol):
    """§5: Callable[[ToolCall, Context], Verdict]."""

    def __call__(self, tool_call: Any, context: Any) -> str: ...


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
