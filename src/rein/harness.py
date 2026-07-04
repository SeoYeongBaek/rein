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

F = TypeVar("F", bound=Callable)


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
        # 이름이 실제로 존재하는지(register_stage 등록 여부)는 register_stage가
        # 인스턴스 메서드라 여기선 아직 알 수 없으므로 _activate에서 확정한다.
        self._stage_order: list[str] = load_stage_order(config)
        self._resolved_stage_order: list[str] | None = None
        self._sealed = False
        # TODO(현준): 인터셉터 / 이벤트 저장소 / 가드레일 집행 엔진 wiring

    def register_stage(self, name: str, fn: StageFn) -> None:
        """§5 스테이지 확장 인터페이스: h.register_stage("safety_v2", my_custom_stage).

        register_tool 데코레이션/__enter__로 하네스가 활성화되기 전까지만
        호출 가능하다. 활성화 이후에는 stage_order가 이미 확정(seal)되었으므로
        조용히 무시하는 대신 즉시 에러를 던진다 — fail-closed.
        """
        if self._sealed:
            raise RuntimeError(
                "register_stage는 register_tool 데코레이션/__enter__ 이전에만 호출 가능합니다."
            )
        self._custom_stages[name] = fn

    def _activate(self) -> None:
        """stage_order를 확정(seal)한다.

        §5 "Harness() 생성 자체를 즉시 실패"의 실질적 의도는 실제 도구 실행
        (런타임) 이전에 막는다는 것이다. register_stage가 인스턴스 메서드라
        __init__ 실행 도중에는 커스텀 스테이지가 등록될 수 없으므로, 도구가
        실제로 실행되기 전 가장 이른 시점(register_tool 데코레이션 또는
        __enter__)에 검증한다. 멱등(idempotent) — 이미 봉인됐으면 재실행하지 않는다.
        """
        if self._sealed:
            return
        self._resolved_stage_order = resolve_stage_order(self._stage_order, self._custom_stages)
        self._sealed = True

    def register_tool(self, func: F) -> F:
        """도구 정의에 붙이는 데코레이터. 인터셉터의 단일 길목을 통과시킨다."""
        # M1 스코프 제약(§4): 동시 호출이 record/replay-verify 사이에서
        # 완료 순서가 달라지면 §6 위치 기반 매칭이 깨지므로, 감지해서
        # 처리하는 대신 애초에 등록을 막아 문제 자체를 스코프 아웃한다.
        if inspect.iscoroutinefunction(func):
            raise TypeError("M1은 동기 함수만 지원합니다")
        self._activate()
        # TODO(현준): 실제 인터셉션 로직. §3 표: 도구 래핑 = 집행 가능(권장).
        # self._resolved_stage_order를 순서대로 순회하며 첫 non-allow 승리.
        raise NotImplementedError

    def observe_model(self, client: Any) -> None:
        """모델 클라이언트 관측을 명시적으로 켠다(§3, 기본 비활성/옵트인).

        이 메서드를 호출하지 않는 한 관측은 시작되지 않는다. 어댑터
        인식 조건(§3)은 내장 타입 자동 감지 OR extract_tool_calls
        최소 프로토콜이며, 둘 다 불만족하면 §5 stage_order와 같은
        fail-closed 정신으로 즉시 에러.
        """
        if not is_recognized_adapter(client):
            raise TypeError(
                f"observe_model: {type(client)!r}는 인식된 어댑터가 아닙니다. "
                "내장 타입(OpenAI/Anthropic/로컬)도 아니고 "
                "extract_tool_calls(response) 메서드도 구현하지 않았습니다."
            )
        self._observed_client = client
        # TODO(현준): 실제 _observe 배선(모델 클라이언트 메서드 패치 →
        # 이벤트 기록). adapters/events 모듈 구현 이후. §3 표: 관측
        # 전용, 집행 불가 — 판정을 되돌리는 코드가 여기 들어가면 안 된다.
        raise NotImplementedError

    def __enter__(self) -> Harness:
        # 방안 B(§4): with Harness(...) as h: agent.run(...) 로 에이전트
        # 루프 전체를 감싸는 진입점. 여기서 켜는 것은 이벤트 저장소(§6)
        # 수명 주기뿐이다. 모델 클라이언트 관측(_observe)은 자동으로
        # 켜지 않는다 — §3 "기본 비활성/옵트인"이므로 관측이 필요하면
        # with 블록 안에서 별도로 observe_model(client)을 호출해야 한다.
        self._activate()
        # TODO(현준): 이벤트 저장소(append-only JSONL) 핸들 준비/wiring.
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # TODO(현준): 이벤트 저장소 flush/close. observe_model()로 등록된
        # 클라이언트가 있다면 여기서 원상 복구/정리한다. __exit__은
        # 정리 전용이지 집행 지점이 아니다.
        return None
