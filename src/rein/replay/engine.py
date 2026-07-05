"""리플레이 엔진 3모드 (CLAUDE.md §6).

record        — 정상 실행 중 녹화 (EventRecorder 위임, 엔진은 모드 인식만)
replay-verify — JSONL에서 읽어 인터셉터에 재통과, LLM/실제 도구 호출 없음
live-rerun    — 실제 도구 재실행, position 매칭으로 녹화 시퀀스와 동기화

인자 매칭 규칙 (CLAUDE.md §6):
- position(seq 인덱스) 기반, 값 비교 없음
- tool_name 불일치 → 즉시 하드 에러
- args 키 집합 불일치 → 즉시 하드 에러
- source: model_client 이벤트는 seq 없음, 매칭 대상 제외
"""

from __future__ import annotations

import copy
import json
import warnings
from pathlib import Path
from typing import Any, Literal


class ReplayMismatchError(Exception):
    pass


def _load_tool_wrap_events(path: Path) -> list[dict[str, Any]]:
    """JSONL에서 source=tool_wrap 이벤트만 로드. model_client는 제외.
    JSONL 파일을 한 줄씩 읽어서 source:tool-wrap인 것만 리스트로 반환"""
    events = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                evt = json.loads(stripped)
            except json.JSONDecodeError as e:
                raise ReplayMismatchError(f"{path}:{line_no} JSONL 파싱 실패: {e}") from e
            if evt.get("source") == "tool_wrap":
                events.append(evt)
    return events


class ReplayEngine:
    """VCR 패턴 자체 구현 (vcrpy 미사용, CLAUDE.md §6)."""

    """모드에 따라 다르게 초기화
    record는 이벤트 로드 안 함, 엔진은 모드 인식만
    replay-verify는 JSONL로드
    live-rerun은 JSONL로드 + 한계 경고 출력"""

    def __init__(
        self,
        log_path: str | Path,
        mode: Literal["record", "replay-verify", "live-rerun"] = "replay-verify",
    ) -> None:
        self._log_path = Path(log_path)
        self._mode = mode
        self._cursor = 0
        self._recorded: list[dict[str, Any]] = []

        if mode in ("replay-verify", "live-rerun"):
            self._recorded = _load_tool_wrap_events(self._log_path)

        if mode == "live-rerun":
            warnings.warn(
                "live-rerun 모드: 정직한 한계 — "
                "깨끗한 정량 A/B는 첫 개입 지점까지만 성립합니다. (CLAUDE.md §6)",
                stacklevel=2,
            )

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def recorded(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._recorded)

    def match(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Position 기반 매칭. 값 비교 없음 (CLAUDE.md §6).

        record 모드에서는 호출 불가 — 녹화는 EventRecorder가 담당한다.
        tool_name 또는 args 키 집합 불일치 시 즉시 ReplayMismatchError.
        """
        if self._mode == "record":
            raise RuntimeError("record 모드에서는 match()를 호출하지 않는다")

        if self._cursor >= len(self._recorded):
            raise ReplayMismatchError(
                f"리플레이 이벤트 소진: position={self._cursor}, " f"총 {len(self._recorded)}개"
            )

        recorded = self._recorded[self._cursor]

        # tool_name 불일치 = 즉시 하드 에러 (로그-실행 순서 어긋남)
        if recorded.get("tool_name") != tool_name:
            raise ReplayMismatchError(
                f"tool_name 불일치 (position={self._cursor}): "
                f"기록={recorded.get('tool_name')!r}, 실행={tool_name!r}"
            )

        # 키 집합 sanity check — 값 비교 금지 (CLAUDE.md §6)
        recorded_keys = sorted((recorded.get("args") or {}).keys())
        live_keys = sorted(args.keys())
        if recorded_keys != live_keys:
            raise ReplayMismatchError(
                f"args 키 집합 불일치 (position={self._cursor}): "
                f"기록={recorded_keys}, 실행={live_keys}"
            )

        self._cursor += 1
        # 내부 상태(self._recorded)의 참조가 아니라 복사본을 반환한다 —
        # 호출부가 반환값을 변형해도 다음 match()/재사용에 영향이 없어야 한다.
        return copy.deepcopy(recorded)

    def __iter__(self):
        """replay-verify: 기록된 tool_wrap 이벤트를 순서대로 순회."""
        return iter(self._recorded)

    def __len__(self) -> int:
        return len(self._recorded)
