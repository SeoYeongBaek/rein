"""Harness: 공개 API 표면.

CLAUDE.md §4 확정 시그니처. 이 파일의 인터페이스(메서드 이름, 인자,
컨텍스트 매니저 프로토콜)는 서영이 동결한다. 내부 구현(인터셉터
연결, 이벤트 기록)은 현준이 채운다.

- register_tool: 도구 "정의"에 데코레이터 한 번 (방안 A, 집행 가능)
- __enter__/__exit__: 컨텍스트 매니저로 루프 전체 감싸기 (방안 B)
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

F = TypeVar("F", bound=Callable)


class Harness:
    def __init__(self, record: str | Path, rules: str | Path | None = None) -> None:
        """
        Args:
            record: 이벤트를 append-only JSONL로 기록할 경로.
            rules: provenance 박힌 YAML 룰셋 경로 (없으면 기본 정책 번들만 적용).
        """
        self.record_path = Path(record)
        self.rules_path = Path(rules) if rules else None
        # TODO(현준): 인터셉터 / 이벤트 저장소 / 가드레일 파이프라인 wiring

    def register_tool(self, func: F) -> F:
        """도구 정의에 붙이는 데코레이터. 인터셉터의 단일 길목을 통과시킨다."""
        # TODO(현준): 실제 인터셉션 로직. §3 표: 도구 래핑 = 집행 가능(권장).
        raise NotImplementedError

    def __enter__(self) -> Harness:
        # TODO(현준): 모델 클라이언트 래핑 진입점 연결 (관측 전용, §3 표 참고)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # TODO(현준): 정리 및 flush
        return None
