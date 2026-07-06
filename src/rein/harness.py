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
from typing import Any, TypeVar

from rein.adapters import is_recognized_adapter
from rein.guardrails import StageFn, load_stage_order, resolve_stage_order
from rein.guardrails.exceptions import ApprovalRequired, Denied, RetryRequested
from rein.guardrails.verdict import Verdict

F = TypeVar("F", bound=Callable)


class Context:
    """도구 호출 컨텍스트 (이후 예산 누적, 에이전트 역할 등을 저장)"""

    pass


# Verdict 문자열 → 예외 클래스 매핑. §4 비-silent 차단 계약을 한 자리에 둔다.
_VERDICT_TO_EXCEPTION: dict[str, Callable[..., Exception]] = {
    "deny": Denied,
    "retry": RetryRequested,
    "approve": ApprovalRequired,
}


def _enforce(verdict: Verdict, rule_id: str, rationale: str, evt_id: str) -> None:
    """non-allow 판정을 예외로 환원. 조용한 차단 금지(§5 fail-closed)."""
    if verdict == Verdict.ALLOW:
        return
    exc_cls = _VERDICT_TO_EXCEPTION[str(verdict)]
    raise exc_cls(str(verdict), rule_id, rationale, evt_id)


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

        # 위치/키워드 인자를 한 dict로 합쳐 §9 `args`와 §6 키 집합 sanity
        # check에 정직한 형태로 만든다(§3 표면 — _intercept가 도구 호출을
        # 있는 그대로 본다).
        try:
            sig = inspect.signature(func)
        except (TypeError, ValueError):
            sig = None

        def _bound_args(args: tuple, kwargs: dict) -> dict[str, Any]:
            if sig is None:
                return dict(kwargs)  # 시그니처를 못 읽으면 키워드만으로 기록
            try:
                bound = sig.bind(*args, **kwargs)
                return dict(bound.arguments)
            except TypeError:
                # 바인딩 실패(잘못된 호출)는 가드레일에 맡기지 말고 그대로 전파 —
                # _intercept는 정상 호출을 모델링하므로 여기선 합치기만 시도
                return dict(kwargs)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = None  # 추후 Context() 객체 연동 시 수정
            bound = _bound_args(args, kwargs)
            tool_call = {"name": func.__name__, "args": bound}

            # 검사 + 실행 진행
            return self._intercept(tool_call, lambda: func(*args, **kwargs), ctx)

        return wrapper  # type: ignore

    def observe_model(self, client: Any) -> None:
        """모델 클라이언트 관측을 명시적으로 켠다(§3, 기본 비활성/옵트인).

        §3 fail-closed: 어댑터 인식 검증은 _observe 진입 "전"에만. 검증
        통과해야만 self._observed_client가 세팅되어 _observe()의 if문이
        풀린다. _intercept 안에서 호출하면 표면 분리가 무너지므로 의도적으로
        호출하지 않는다.
        """
        if not is_recognized_adapter(client):
            raise TypeError(
                f"observe_model: {type(client)!r}는 인식된 어댑터가 아닙니다. "
                "내장 타입(OpenAI/Anthropic/로컬)도 아니고 "
                "extract_tool_calls(response) 메서드도 구현하지 않았습니다."
            )
        self._observed_client = client

    def _intercept(
        self,
        tool_call: dict[str, Any],
        do_call: Callable[[], Any],
        ctx: Context | None,
    ) -> Any:
        """집행 표면(§3 표, 권장, 강제 집행 경로).

        도구 실행 직전에 가드레일 파이프라인을 돌리고, 첫 non-allow에서
        즉시 예외를 던진다(§5 short-circuit, §4 비-silent 차단). 통과한
        경우에만 do_call을 실행한다 — 이 한 자리가 "집행 여부" 결정의
        유일한 지점이며, _observe와 책임이 겹치지 않는다(§3 표면 분리).

        Raises:
            Denied | RetryRequested | ApprovalRequired: 첫 non-allow 판정.
        """
        pipeline = self._sealed_pipeline()  # _activate() 완료 후에만 유효.

        # ① 검사: 첫 non-allow 승리(§5).
        for _stage_name, stage_fn in pipeline:
            verdict, rule_id, rationale, _ = stage_fn(tool_call, ctx)
            if verdict != Verdict.ALLOW:
                # 예외로 환원 — 원본 도구는 호출되지 않음(§4).
                _enforce(verdict, rule_id, rationale, evt_id="")
                return  # type: ignore[unreachable]

        # ② 집행: 통과한 경우에만 기록 + 실행.
        #    §6 매칭 키 seq는 _record_tool_wrap_event 내부에서 부여한다.
        self._record_tool_wrap_event(
            {
                "tool_name": tool_call["name"],
                "args": tool_call.get("args", {}),
                "verdict": "allow",
                "ctx": ctx,
            }
        )
        return do_call()

    def _observe(self, response_or_event: Any) -> None:
        """관측 표면(§3 표, 옵트인, default-off).

        LLM이 제안한 tool_use를 *기록만* 한다. 판정을 내리거나 실행을 가로채지
        않는다 — 집행 불가능한 표면(§3)에 가드레일 판정을 걸면 "막아줄 것"
        같은 거짓 안전감을 주기 때문이다. 옵트인 정책(§3)에 따라
        self._observed_client가 없으면 아무 동작도 하지 않는다.

        PR #4 범위: default-off 동결만. 옵트인 시 실제 모델 응답을 가로채는
        패치는 후속 PR의 wiring 책임이며, 그 PR에서 self._observed_client로
        저장된 클라이언트의 응답을 여기 _observe()로 흘려보낸다.
        """
        if self._observed_client is None:
            return  # §3 default-off — 명시적 활성화 없이는 절대 실행되지 않음.

        # TODO(후속 PR): 어댑터를 통해 tool_use를 추출하고 저장소에 기록.
        #               §9 스키마상 source=model_client, verdict=null,
        #               parent_seq=선행 tool_wrap의 seq, seq 자체는 null.

    def _sealed_pipeline(self) -> list[tuple[str, StageFn]]:
        """_activate()에서 확정된 stage_order를 (name, fn) 페어로 노출.

        본 PR에서는 최소 더미: 내장 4스테이지 + 등록된 커스텀 스테이지를
        순서대로 묶어 반환한다. 실제 stage fn 시그니처는 §5의
        Callable[[ToolCall, Context], Verdict]를 따른다. 후속 PR(record wiring
        / SQL featurizer)에서 같은 시그니처를 유지한 채 구현이 채워진다.
        """
        if not self._resolved_stage_order:
            return []

        pipeline: list[tuple[str, StageFn]] = []
        for name in self._resolved_stage_order:
            fn = self._custom_stages.get(name) or self._default_stages.get(name)
            if fn is None:
                # §5 fail-closed: seal 단계에서 미등록 이름을 잡지 못한 경우의
                # 마지막 방어선. 정상 흐름에선 _activate()에서 이미 잡힌다.
                raise ValueError(f"정의되지 않은 스테이지입니다: {name}")
            pipeline.append((name, fn))
        return pipeline

    def _record_tool_wrap_event(self, event: dict[str, Any]) -> None:
        """tool_wrap 이벤트 기록. 본 PR에서는 최소 더미 (no-op).

        §9 스키마상 source=tool_wrap, seq=단일 순번 카운터, verdict 등.
        후속 PR(append-only JSONL 저장소 #6 / record wiring)에서 실제 구현이
        들어온다. 본 PR은 _intercept의 "집행 표면" 책임 동결이 목적이므로
        기록은 빈 껍데기로 둔다 — seq 카운터 도입은 record wiring PR 책임.
        """
        # TODO(junn1104, #6 JSONL 저장소 PR): append + seq 카운터 부여.
        pass

    # --- 기본 스테이지 더미 구현체 (반환값: Verdict, rule_id, rationale, evt_id) ---
    def _default_schema_check(
        self, tool_call: dict[str, Any], ctx: Any
    ) -> tuple[Verdict, str, str, str]:
        return Verdict.ALLOW, "", "", ""

    def _default_permission_check(
        self, tool_call: dict[str, Any], ctx: Any
    ) -> tuple[Verdict, str, str, str]:
        return Verdict.ALLOW, "", "", ""

    def _default_budget_check(
        self, tool_call: dict[str, Any], ctx: Any
    ) -> tuple[Verdict, str, str, str]:
        return Verdict.ALLOW, "", "", ""

    def _default_safety_check(
        self, tool_call: dict[str, Any], ctx: Any
    ) -> tuple[Verdict, str, str, str]:
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
