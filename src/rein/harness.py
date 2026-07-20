"""Harness: 공개 API 표면.

CLAUDE.md §4 확정 시그니처. 이 파일의 인터페이스(메서드 이름, 인자,
컨텍스트 매니저 프로토콜)는 서영이 동결한다. 내부 구현(인터셉터
연결, 이벤트 기록)은 현준이 채운다.

- register_tool: 도구 "정의"에 데코레이터 한 번 (방안 A, 집행 가능)
- __enter__/__exit__: 컨텍스트 매니저로 루프 전체 감싸기 (방안 B)

[이슈 #65] §5 세션 누적 상태와 §9 로그 정적 메타데이터 분리.

#64 결정 (b) 구현: register_tool wrapper가 stage 함수에는 Harness
내부 _session_state dict(누적 가능, §5 budget stage PR이 채울 예정)
를, record_tool_wrap에는 self._context의 호출 시점 얕은 복사본(§9
정적 메타데이터 보호)을 별개 객체로 전달한다. stage 함수가 ctx를
mutate해도 그 mutate된 상태가 이번 호출의 tool_wrap 로그 줄에
새어 들어가지 않는다. 사용자 공개 API(Harness(context=dict|None))
시그니처는 변경 없음.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypeVar

from rein.adapters import is_recognized_adapter
from rein.events import SEVERITY_WARNING, EventStore
from rein.guardrails import StageFn, load_stage_order, resolve_stage_order
from rein.guardrails.exceptions import ApprovalRequired, Denied, RetryRequested
from rein.guardrails.verdict import Verdict
from rein.replay import ReplayEngine

F = TypeVar("F", bound=Callable)


class Context:
    """도구 호출 컨텍스트. stage 함수가 받는 ctx의 정식 타입 자리.

    [이슈 #65] 본 클래스는 §5에서 약속한 Context 시그니처의 최소 스텁이다.
    실제 필드/메서드(예: budget 카운터, 토큰 누적)는 budget stage PR(§12
    M4 후보)의 책임이며, 그 PR에서 본 클래스를 채우거나 self._session_state
    를 본 클래스로 감싸는 결정이 이뤄진다. 현 PR은 본 클래스의 본체를
    열지 않고 signature만 유지한다.

    stage가 실제로 받는 ctx 객체는 self._session_state dict다 — 본 PR
    결정(이슈 #65 follow-up)에 따라 사용자 context의 얕은 복사본으로
    seed되며, 그 위에 동적 누적(counter, token 등)이 얹힌다. 본 클래스는
    그 dict의 향후 타입 자리 표시 역할만 한다(M4 budget stage PR이 본
    클래스를 열 때 seed/dynamic 누적을 어떻게 노출할지 거기서 결정).
    """

    pass


# Verdict 문자열 → 예외 클래스 매핑. §4 비-silent 차단 계약을 한 자리에 둔다.
_VERDICT_TO_EXCEPTION: dict[Verdict, Callable[..., Exception]] = {
    Verdict.DENY: Denied,
    Verdict.APPROVE: ApprovalRequired,
    Verdict.RETRY: RetryRequested,
}


def _enforce(verdict: Verdict, rule_id: str, rationale: str, evt_id: str) -> None:
    """non-allow 판정을 예외로 환원. 조용한 차단 금지(§5 fail-closed)."""
    if verdict == Verdict.ALLOW:
        return
    exc_cls = _VERDICT_TO_EXCEPTION[verdict]
    raise exc_cls(str(verdict), rule_id, rationale, evt_id)


def _snapshot_context_for_log(ctx: Any) -> dict[str, Any]:
    """[이슈 #65] §9 보호용 얕은 복사.

    EventStore._serialize_context가 이미 dict(ctx) 얕은 복사를 하긴 하지만,
    그 시점은 record_tool_wrap 내부 (이미 _serialize_context에 인자로
    들어온 ctx가 post-mutation 상태인 경우)다. wrapper는 stage가 mutate
    하기 "전"의 사용자 의도 시점의 스냅샷을 _serialize_context에 넘겨야
    한다 — 그래서 stage_ctx와 log_ctx를 분리해 wrapper에서 복사한다.

    _serialize_context의 직렬화 규칙(None / dict / __dict__)과 동일한
    분류를 그대로 따른다(중복 정의지만, "이게 wrapper 단계의 얕은
    복사"라는 의도를 코드에 박기 위해 명시).
    """
    if ctx is None:
        return {}
    if isinstance(ctx, dict):
        return dict(ctx)
    return dict(getattr(ctx, "__dict__", {}) or {})


class Harness:
    def __init__(
        self,
        record: str | Path,
        rules: str | list[str] | None = None,
        config: str = "rein.yaml",
        mode: Literal["record", "live-rerun"] = "record",
        replay_from: str | Path | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """
        Args:
            record: 이벤트를 append-only JSONL로 기록할 경로.
            rules: provenance 박힌 YAML 룰셋 경로. 리스트로 여러 파일 조합 가능.
            config: stage_order 등 파이프라인 설정 파일 경로. cwd 자동 탐색.
            mode: "record"(기본) 또는 "live-rerun". replay-verify는 실도구
                호출이 없어(§6) Harness를 거치지 않고 CLI(`rein replay`)가
                단독 수행하므로 여기 없다.
            replay_from: mode="live-rerun"일 때 재생할 run.jsonl 경로.
                live-rerun은 실제 도구 함수가 사용자 프로세스 안에만
                있어 CLI가 대신 실행할 수 없다(§4) — 그래서 사용자가
                자기 스크립트를 다시 실행하며 여기로 트리거한다.
            context: 모든 도구 호출의 가드레일 검사와 이벤트 기록에 전달할
                선택적 실행 컨텍스트. 예: {"agent_role": "content_editor"}.
                지정하지 않으면 기존처럼 빈 context로 기록된다.
        """
        # §5와 동일한 fail-closed 패턴: 잘못된 조합은 생성 시점에 즉시 에러.
        if mode == "live-rerun" and replay_from is None:
            raise ValueError('mode="live-rerun"이면 replay_from을 반드시 지정해야 합니다.')
        if mode == "record" and replay_from is not None:
            raise ValueError('replay_from은 mode="live-rerun"일 때만 사용합니다.')

        self.record_path = Path(record)
        # §6/§9: 모든 tool_wrap + outcome 이벤트는 이 저장소를 통해서만 append된다.
        self._event_store = EventStore(self.record_path)
        self.rules = rules
        self.config = config
        self.mode = mode
        self.replay_from = Path(replay_from) if replay_from is not None else None
        # §9: 정적 호출 메타데이터로 로그에 기록되는 사용자 context.
        #     stage 함수가 이 dict 자체를 mutate해선 안 된다(§5/§9 분리).
        #     log 경로(record_tool_wrap) 전용 — §9 정적 메타 약속 보존용.
        self._context = context
        # [이슈 #65 follow-up] §5: stage가 받을 ctx. 사용자 context의
        # 얕은 복사본으로 seed해 시작한다 — budget stage가 agent_role
        # 같은 정적 메타데이터를 자연스럽게 읽을 수 있도록 (§5 "state는
        # 시그니처에 드러난 의존성" 준수. stage가 stage_ctx 바깥의
        # self._context를 직접 참조하는 §5 위반 경로를 원천 차단).
        # 동적 누적(counter, token 등)은 그 위에 얹힌다. 사용자 원본
        # context는 §9 정적 메타로 따로 보존되어, session state의
        # mutation이 §9 로그 context 필드를 오염시키지 않는다. budget
        # stage PR(§12 M4 후보)이 동적 누적을 채울 예정 — 본 PR은
        # seed + §5/§9 분리만 담당.
        self._session_state: dict[str, Any] = dict(self._context) if self._context else {}
        self._observed_client: Any | None = None  # §3: 기본 비활성
        self._custom_stages: dict[str, StageFn] = {}
        # live-rerun: 실제 함수 호출 직전 위치 매칭(§6)에 쓸 엔진.
        # record 모드에서는 None으로 두어 _intercept가 match()를 건너뛴다.
        self._replay_engine: ReplayEngine | None = None
        if mode == "live-rerun":
            self._replay_engine = ReplayEngine(self.replay_from, mode="live-rerun")

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

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # [이슈 #65] §5/§9 분리. 두 객체는 서로 다른 dict다.
            # · stage_ctx: §5 세션 누적 상태. __init__에서 사용자
            #   context의 얕은 복사로 seed됨. stage 함수가 agent_role
            #   같은 정적 메타를 읽을 수 있고, 그 위에 counter·token
            #   같은 동적 누적을 얹는다.
            # · log_ctx: §9 정적 메타데이터. self._context의 호출 시점
            #   얕은 복사본. stage_ctx의 mutation이 log로 새지 않도록
            #   wrapper에서 미리 스냅샷.
            # 분리하지 않으면 stage의 session mutation이 이번 호출의
            # tool_wrap 로그 줄에 그대로 새어 들어간다 (이슈 #64 #65 배경).
            stage_ctx = self._session_state
            log_ctx = _snapshot_context_for_log(self._context)

            bound = _bound_args(args, kwargs)
            tool_call = {"name": func.__name__, "args": bound}

            # 검사 + 실행 진행
            return self._intercept(tool_call, lambda: func(*args, **kwargs), stage_ctx, log_ctx)

        return wrapper  # type: ignore

    def observe_model(self, client: Any) -> None:
        """모델 클라이언트 관측을 명시적으로 켠다(§3, 기본 비활성/옵트인).

        §3 fail-closed: 어댑터 인식 검증은 _observe 진입 "전"에만. 검증
        통과해야만 self._observed_client가 세팅되어 _observe()의 if문이
        풀린다.
        """
        if not is_recognized_adapter(client):
            raise TypeError(
                f"observe_model: {type(client)!r}는 인식된 어댑터가 아닙니다. "
                "내장 타입(OpenAI/Anthropic 모듈 prefix)도 아니고 "
                "호출 가능한 extract_tool_calls(response) 메서드도 구현하지 않았습니다. "
                "로컬 클라이언트는 §3 TODO에 따라 자동 감지 대상이 아니므로 "
                "extract_tool_calls(response)를 직접 구현해야 합니다."
            )
        self._observed_client = client

    def _intercept(
        self,
        tool_call: dict[str, Any],
        do_call: Callable[[], Any],
        stage_ctx: dict[str, Any] | None,
        log_ctx: dict[str, Any] | None,
    ) -> Any:
        """집행 표면(§3 표, 권장, 강제 집행 경로).

        도구 실행 직전에 가드레일 파이프라인을 돌리고, 첫 non-allow에서
        즉시 예외를 던진다(§5 short-circuit, §4 비-silent 차단). 통과한
        경우에만 do_call을 실행한다 — 이 한 자리가 "집행 여부" 결정의
        유일한 지점이며, _observe와 책임이 겹치지 않는다(§3 표면 분리).

        mode="live-rerun"이면 실제 함수 호출 직전에 ReplayEngine.match()로
        녹화 시퀀스와의 위치 매칭을 검증한다(§6). 매칭 실패는
        ReplayMismatchError로 그대로 전파된다 — 가드레일이 이전 실행과
        다른 지점에서 개입하면 그 이후 위치 매칭이 깨지는 것 자체가
        §6 "정직한 한계"의 관측 결과이므로 여기서 흡수하지 않는다.

        [이슈 #65] 인자 분리: stage_ctx(§5 세션 누적 상태, 가변)와
        log_ctx(§9 정적 메타데이터, 호출 시점 얕은 복사)를 별개로
        받는다. stage_ctx는 stage 함수에, log_ctx는 record_tool_wrap에
        각각 전달되어 stage의 mutation이 이번 호출 로그에 새지 않는다.

        Args:
            tool_call: {"name": str, "args": dict} 형태의 호출 정보.
            do_call: 실제 도구 함수를 호출하는 no-arg callable.
            stage_ctx: §5 세션 누적 상태. stage 함수에 그대로 전달.
            log_ctx: §9 정적 메타데이터. record_tool_wrap에 그대로 전달.

        Raises:
            Denied | RetryRequested | ApprovalRequired: 첫 non-allow 판정.
            ReplayMismatchError: live-rerun 위치 매칭 실패.
        """
        pipeline = self._sealed_pipeline()  # _activate() 완료 후에만 유효.

        # ① 검사: 첫 non-allow 승리(§5). stage_ctx가 stage에 직접 전달.
        for _stage_name, stage_fn in pipeline:
            verdict, rule_id, rationale, evt_id = stage_fn(tool_call, stage_ctx)
            if verdict != Verdict.ALLOW:
                # 예외로 환원 — 원본 도구는 호출되지 않음(§4).
                _enforce(verdict, rule_id, rationale, evt_id=evt_id)
                return  # type: ignore[unreachable]

        # ② live-rerun 위치 매칭: 실제(부작용 있는) 함수 호출보다 먼저,
        #    녹화된 시퀀스의 같은 자리인지 확인한다(§6 인자 매칭 규칙).
        if self._replay_engine is not None:
            self._replay_engine.match(tool_call["name"], tool_call.get("args", {}))

        # ③ 집행: 통과한 경우에만 기록 + 실행.
        #    [이슈 #65] log_ctx는 호출 시점 얕은 복사본 — stage가
        #    stage_ctx를 mutate해도 이번 로그 줄에 영향 없음.
        #    §6 매칭 키 seq는 EventStore.record_tool_wrap 내부에서 부여한다.
        event = self._event_store.record_tool_wrap(
            tool_name=tool_call["name"],
            args=tool_call.get("args", {}),
            context=log_ctx,
            verdict="allow",
        )
        try:
            result = do_call()
        except Exception as exc:
            # §7 분류 테이블(SQL featurize 등)은 M2 스코프. 다운스트림
            # 규칙 엔진(rules/__init__.py)은 이 값을 신뢰하지 않고
            # 항상 featurize로 재계산하므로, M1은 의식적으로 선택한
            # 고정값(warning)만 채운다(§37 — 조용한 기본값 금지는
            # EventStore API 쪽 계약이지 호출자의 판단까지 금지하지 않음).
            self._event_store.record_error(event, exc, severity=SEVERITY_WARNING)
            raise
        else:
            self._event_store.record_ok(event)
            return result

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
        # EventStore는 lazy open(첫 record_* 호출 시점에 파일이 열림)이라
        # 여기서 별도로 열 것은 없다.
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        # TODO(현준): observe_model()로 등록된 클라이언트가 있다면 여기서
        # 원상 복구/정리한다. __exit__은 정리 전용이지 집행 지점이 아니다.
        self._event_store.close()
        return None
