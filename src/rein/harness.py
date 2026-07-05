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

from typing import Any, Dict, Tuple, TypeVar

from rein.adapters import is_recognized_adapter
from rein.guardrails import StageFn, load_stage_order, resolve_stage_order
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
        rules: str | list[str] | None = None,
        config: str = "rein.yaml",
    ) -> None:
        """
        Args:
            record: 이벤트를 append-only JSONL로 기록할 경로.
            rules: provenance 박힌 YAML 룰셋 경로. 리스트로 여러 파일 조합 가능.
            config: stage_order 등 파이프라인 설정 파일 경로. cwd 자동 탐색.
        """
        self.record_path = Path(record)
        self.rules = rules
        self.config = config
        self._observed_client: Any | None = None  # §3: 기본 비활성
        self._custom_stages: dict[str, StageFn] = {}
        
        # §5 fail-closed: 구조(YAML 파싱/타입) 검증은 생성 시점에 즉시 한다.
        self._stage_order: list[str] = load_stage_order(config)
        self._resolved_stage_order: list[str] | None = None
        self._sealed = False

        # --- [현준 구현] 기본 4단계 스테이지 등록 ---
        self._default_stages: dict[str, StageFn] = {
            "schema": self._default_schema_check,
            "permission": self._default_permission_check,
            "budget": self._default_budget_check,
            "safety": self._default_safety_check,
        }

    def register_stage(self, name: str, fn: StageFn) -> None:
        """§5 스테이지 확장 인터페이스"""
        if self._sealed:
            raise RuntimeError(
                "register_stage는 register_tool 데코레이션/__enter__ 이전에만 호출 가능합니다."
            )
        self._custom_stages[name] = fn

    def _activate(self) -> None:
        """stage_order를 확정(seal)한다."""
        if self._sealed:
            return
        self._resolved_stage_order = resolve_stage_order(self._stage_order, self._custom_stages)
        self._sealed = True

    def register_tool(self, func: F) -> F:
        """도구 정의에 붙이는 데코레이터. 인터셉터의 단일 길목을 통과시킨다."""
        if inspect.iscoroutinefunction(func):
            raise TypeError("M1은 동기 함수만 지원합니다")
            
        # 도구가 실행되기 전 가장 이른 시점에 파이프라인 봉인(seal) 및 확정
        self._activate()
        
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tool_call = {"name": func.__name__, "args": kwargs}
            ctx = None  # 추후 Context() 객체 연동 시 수정
            
            # ① 검사: 가드레일 파이프라인 통과 여부 확인 (현준 로직)
            self._evaluate_pipeline(tool_call, ctx)
            
            # ② 실제 도구 실행
            return func(*args, **kwargs)
            
        return wrapper  # type: ignore

    def _evaluate_pipeline(self, tool_call: Dict[str, Any], ctx: Any) -> None:
        """결정론적 4단계 검사 (Short-circuit 방식)"""
        if not self._resolved_stage_order:
            return

        for stage_name in self._resolved_stage_order:
            # 커스텀 스테이지 우선 확인, 없으면 기본 스테이지 사용
            stage_func = self._custom_stages.get(stage_name) or self._default_stages.get(stage_name)
            
            if not stage_func:
                raise ValueError(f"정의되지 않은 스테이지입니다: {stage_name}")

            verdict, rule_id, rationale, evt_id = stage_func(tool_call, ctx)
            
            # 첫 번째로 나오는 non-allow 판정에서 즉시 파이프라인 종료 (Fail-fast)
            if verdict != Verdict.ALLOW:
                if verdict == Verdict.DENY:
                    raise Denied(str(verdict), rule_id, rationale, evt_id)
                elif verdict == Verdict.APPROVE:
                    raise ApprovalRequired(str(verdict), rule_id, rationale, evt_id)
                elif verdict == Verdict.RETRY:
                    raise RetryRequested(str(verdict), rule_id, rationale, evt_id)

    def observe_model(self, client: Any) -> None:
        """모델 클라이언트 관측을 명시적으로 켠다(§3, 기본 비활성/옵트인)."""
        if not is_recognized_adapter(client):
            raise TypeError(
                f"observe_model: {type(client)!r}는 인식된 어댑터가 아닙니다. "
                "내장 타입(OpenAI/Anthropic/로컬)도 아니고 "
                "extract_tool_calls(response) 메서드도 구현하지 않았습니다."
            )
        self._observed_client = client
        raise NotImplementedError

    # --- 기본 스테이지 더미 구현체 (반환값: Verdict, rule_id, rationale, evt_id) ---
    def _default_schema_check(self, tool_call: Dict[str, Any], ctx: Any) -> Tuple[Verdict, str, str, str]:
        return Verdict.ALLOW, "", "", ""

    def _default_permission_check(self, tool_call: Dict[str, Any], ctx: Any) -> Tuple[Verdict, str, str, str]:
        return Verdict.ALLOW, "", "", ""

    def _default_budget_check(self, tool_call: Dict[str, Any], ctx: Any) -> Tuple[Verdict, str, str, str]:
        return Verdict.ALLOW, "", "", ""

    def _default_safety_check(self, tool_call: Dict[str, Any], ctx: Any) -> Tuple[Verdict, str, str, str]:
        return Verdict.ALLOW, "", "", ""

    def __enter__(self) -> Harness:
        # 방안 B(§4): with Harness(...) as h: agent.run(...) 로 에이전트
        # 루프 전체를 감싸는 진입점. 여기서 켜는 것은 이벤트 저장소(§6)
        # 수명 주기뿐이다. 모델 클라이언트 관측(_observe)은 자동으로
        # 켜지 않는다 — §3 "기본 비활성/옵트인"이므로 관측이 필요하면
        # with 블록 안에서 별도로 observe_model(client)을 호출해야 한다.
        self._activate()
        # TODO(현준): 이벤트 저장소(append-only JSONL) 핸들 준비/wiring.
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        # TODO(현준): 이벤트 저장소 flush/close. observe_model()로 등록된
        # 클라이언트가 있다면 여기서 원상 복구/정리한다. __exit__은
        # 정리 전용이지 집행 지점이 아니다.
        return None