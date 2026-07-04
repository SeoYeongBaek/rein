"""Harness: 공개 API 표면.

CLAUDE.md §4 확정 시그니처. 이 파일의 인터페이스(메서드 이름, 인자,
컨텍스트 매니저 프로토콜)는 서영이 동결한다. 내부 구현(인터셉터
연결, 이벤트 기록)은 현준이 채운다.

- register_tool: 도구 "정의"에 데코레이터 한 번 (방안 A, 집행 가능)
- __enter__/__exit__: 컨텍스트 매니저로 루프 전체 감싸기 (방안 B)
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar, Any, Dict, Tuple

from rein.guardrails.verdict import Verdict
from rein.guardrails.exceptions import Denied, RetryRequested, ApprovalRequired

F = TypeVar("F", bound=Callable)

class Context:
    """도구 호출 컨텍스트 (이후 예산 누적, 에이전트 역할 등을 저장)"""
    pass

class Harness:
    def __init__(
        self, 
        record: str | Path, 
        rules: str | list[str] | None = None,  # 피드백 반영: list[str] 명세 일치
        config: str = "rein.yaml"              # 피드백 반영: 기본값 명세 일치
    ) -> None:
        """
        Args:
            record: 이벤트를 append-only JSONL로 기록할 경로.
            rules: provenance 박힌 YAML 룰셋 경로 (없으면 기본 정책 번들만 적용).
            config: stage_order 등 설정 (기본값 rein.yaml)
        """
        self.record_path = Path(record)
        
        # rules가 리스트로 들어올 수도 있으므로 각각 Path 객체로 변환 처리
        if isinstance(rules, list):
            self.rules_path = [Path(r) for r in rules]
        else:
            self.rules_path = Path(rules) if rules else None
            
        self.config_path = Path(config)
        
        # --- [현준 구현] 가드레일 파이프라인 wiring ---
        self.registered_stages: Dict[str, Callable] = {}
        
        # 1. 기본 4단계 스테이지 파이프라인 등록 (순수 Python 함수)
        self.register_stage("schema", self._default_schema_check)
        self.register_stage("permission", self._default_permission_check)
        self.register_stage("budget", self._default_budget_check)
        self.register_stage("safety", self._default_safety_check)
        
        # 2. 설정된 스테이지 순서 (yaml 파싱 전 임시 하드코딩)
        self.stage_order = ["schema", "permission", "budget", "safety"]
        
        # 3. Fail-Closed 원칙 검증: 미등록 스테이지가 있으면 즉시 실패
        self._validate_stages()

    def _validate_stages(self) -> None:
        for stage in self.stage_order:
            if stage not in self.registered_stages:
                raise ValueError(
                    f"Fail-Closed Error: 미등록 스테이지 '{stage}'가 stage_order에 존재합니다. "
                    f"Harness 초기화를 중단합니다."
                )

    def register_stage(self, name: str, func: Callable) -> None:
        self.registered_stages[name] = func

    def register_tool(self, func: F) -> F:
        """도구 정의에 붙이는 데코레이터. 인터셉터의 단일 길목을 통과시킨다."""
        # M1 스코프 제약: 비동기 함수는 등록 자체를 거부
        if inspect.iscoroutinefunction(func):
            raise TypeError("M1은 동기 함수만 지원합니다")
            
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tool_call = {"name": func.__name__, "args": kwargs}
            ctx = Context()
            
            # ① 검사: 가드레일 파이프라인 통과 여부 확인
            self._evaluate_pipeline(tool_call, ctx)
            
            # ② 기록: 이벤트 저장소 기록 (추후 구현)
            
            # 실제 도구 실행
            return func(*args, **kwargs)
        return wrapper  # type: ignore

    def _evaluate_pipeline(self, tool_call: Dict[str, Any], ctx: Context) -> None:
        """결정론적 4단계 검사 (Short-circuit 방식)"""
        for stage_name in self.stage_order:
            stage_func = self.registered_stages[stage_name]
            verdict, rule_id, rationale, evt_id = stage_func(tool_call, ctx)
            
            # 첫 번째로 나오는 non-allow 판정에서 즉시 파이프라인 종료 (Fail-fast)
            if verdict != Verdict.ALLOW:
                if verdict == Verdict.DENY:
                    raise Denied(str(verdict), rule_id, rationale, evt_id)
                elif verdict == Verdict.APPROVE:
                    raise ApprovalRequired(str(verdict), rule_id, rationale, evt_id)
                elif verdict == Verdict.RETRY:
                    raise RetryRequested(str(verdict), rule_id, rationale, evt_id)

    # --- 기본 스테이지 더미 구현체 (반환값: Verdict, rule_id, rationale, evt_id) ---
    def _default_schema_check(self, tool_call: Dict[str, Any], ctx: Context) -> Tuple[Verdict, str, str, str]:
        return Verdict.ALLOW, "", "", ""

    def _default_permission_check(self, tool_call: Dict[str, Any], ctx: Context) -> Tuple[Verdict, str, str, str]:
        return Verdict.ALLOW, "", "", ""

    def _default_budget_check(self, tool_call: Dict[str, Any], ctx: Context) -> Tuple[Verdict, str, str, str]:
        return Verdict.ALLOW, "", "", ""

    def _default_safety_check(self, tool_call: Dict[str, Any], ctx: Context) -> Tuple[Verdict, str, str, str]:
        return Verdict.ALLOW, "", "", ""

    def __enter__(self) -> Harness:
        # TODO(현준): 모델 클라이언트 래핑 진입점 연결 (관측 전용, §3 표 참고)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        # TODO(현준): 정리 및 flush
        return None