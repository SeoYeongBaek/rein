# register_tool 비동기 도구 지원 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `Harness.register_tool`이 `async def` 도구를 받아들여 순차적으로(한 번에 하나씩 `await`) 실행할 수 있게 하되, `asyncio.gather` 등으로 실제 동시 호출되면 `ConcurrentToolCallError`로 즉시 거부해 §6 위치 기반 리플레이 매칭을 무변경으로 보존한다.

**Architecture:** `_intercept`의 "검사(가드레일 스테이지 순회) + tool_wrap 기록 + live-rerun 위치 매칭" 부분을 `_run_guardrail_stages` 헬퍼로 추출해 sync(`_intercept`)와 신규 async(`_intercept_async`) 양쪽이 공유한다. `_intercept_async`는 `asyncio.Lock`으로 "검사 시작 → seq 부여 → 실행 → outcome 기록" 구간 전체를 원자적 단위로 묶어, seq 부여 순서가 항상 호출 순서와 일치하게 만든다. `register_tool`은 `inspect.iscoroutinefunction`으로 분기해 sync/async 각각의 wrapper를 반환한다.

**Tech Stack:** Python 3.11+ 표준 라이브러리 `asyncio`만 사용. 신규 외부 의존성 없음(테스트도 `pytest-asyncio` 없이 `asyncio.run()`으로 감싼 일반 `def test_*`로 작성).

## Global Constraints

- Python `>=3.11` (pyproject.toml) — `asyncio.Lock()`은 실행 중인 이벤트 루프 없이 생성 가능.
- 신규 외부 의존성 추가 금지 (`pytest-asyncio` 포함) — 비동기 테스트는 `asyncio.run()`으로 감싼 일반 `def` 함수로 작성.
- **순차 async만 지원한다.** `asyncio.gather` 등으로 등록된 도구를 실제 동시에 여러 개 실행하는 것은 `ConcurrentToolCallError`로 거부한다(조용한 직렬화 금지 — fail-closed).
- `ReplayEngine`(`src/rein/replay/engine.py`), `EventStore`(`src/rein/events/event_store.py`), §9 이벤트 스키마는 이번 작업에서 **한 줄도 변경하지 않는다.**
- 스코프 밖(이번 계획에 포함하지 않음): `observe_model`의 비동기 모델 클라이언트(AsyncOpenAI/AsyncAnthropic) 자동 배선, `register_stage` 커스텀 가드레일 스테이지의 `async def` 지원, 도구의 진짜 병렬 실행, 멀티스레드(여러 이벤트 루프) 안전성.
- CLAUDE.md는 living file(§14) — `register_tool`의 "M1 스코프 제약: 동기 호출만 지원" 서술(§4)을 구현 완료 사실에 맞게 갱신한다.
- 참고 스펙: `docs/superpowers/specs/2026-08-21-async-tool-support-design.md`

---

## File Structure

| 파일 | 변경 | 책임 |
|---|---|---|
| `src/rein/harness.py` | 수정 | `ConcurrentToolCallError` 정의, `Harness.__init__`에 async 상태 추가, `_run_guardrail_stages` 헬퍼 추출, `_intercept_async` 신규, `register_tool` sync/async 분기 |
| `tests/test_harness_async.py` | 신규 | async 도구 지원 전용 테스트 8건 |
| `tests/test_harness_issue_4.py` | 수정 | 구식 "async 거부" 테스트를 "async 허용" 테스트로 교체 |
| `tests/test_pipeline.py` | 수정 | 동일 |
| `tests/test_smoke.py` | 수정 | 동일 |
| `CLAUDE.md` | 수정 | §4 "M1 스코프 제약 — 동기 호출만 지원" 문단을 구현 완료 사실로 갱신 |

---

### Task 1: `_intercept`에서 `_run_guardrail_stages` 헬퍼 추출 (순수 리팩터, 동작 변경 없음)

**Files:**
- Modify: `src/rein/harness.py:321-405` (`_intercept` 메서드)
- Test: 기존 스위트 전체(`pytest -q`)가 회귀 없이 그대로 통과해야 함 — 이 태스크는 새 테스트를 추가하지 않고 기존 테스트를 안전망으로 쓴다.

**Interfaces:**
- Produces: `Harness._run_guardrail_stages(tool_call: dict[str, Any], stage_ctx: dict[str, Any] | None, log_ctx: dict[str, Any] | None) -> dict[str, Any]` — 통과 시 tool_wrap이 기록된 event dict 반환, non-allow 시 `Denied`/`RetryRequested`/`ApprovalRequired` 예외.

- [ ] **Step 1: 리팩터 전 베이스라인 확인**

Run: `.venv/bin/pytest -q`
Expected: 전체 통과(현재 통과 중인 개수 그대로 기록해둔다 — 이후 비교 기준).

- [ ] **Step 2: `_run_guardrail_stages` 헬퍼 추가 + `_intercept` 축소**

`src/rein/harness.py`의 기존 `_intercept` 메서드(현재 321~405행) 전체를 아래로 교체한다:

```python
    def _run_guardrail_stages(
        self,
        tool_call: dict[str, Any],
        stage_ctx: dict[str, Any] | None,
        log_ctx: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """검사 + tool_wrap 기록 + live-rerun 위치 매칭 (sync/async 공유, M4 #78).

        `_intercept`(sync)와 `_intercept_async`가 공통으로 쓰는 부분만
        추출한 것 — do_call() 실행과 outcome 기록은 호출자가 각자
        담당한다(sync는 즉시 호출, async는 await).

        non-allow 판정은 tool_wrap을 기록한 뒤 예외로 환원해 던진다
        (§4 비-silent 차단, §5 fail-closed). 통과하면 tool_wrap이 기록된
        event dict를 반환한다.
        """
        pipeline = self._sealed_pipeline()  # _activate() 완료 후에만 유효.

        # ① 검사: 첫 non-allow 승리(§5). stage_ctx가 stage에 직접 전달.
        for _stage_name, stage_fn in pipeline:
            verdict, rule_id, rationale, _stage_evt_id = stage_fn(tool_call, stage_ctx)
            if verdict != Verdict.ALLOW:
                # non-allow도 §9 그대로 tool_wrap 한 줄로 남긴다. 실행이
                # 없었으므로 outcome 줄은 만들지 않는다.
                event = self._event_store.record_tool_wrap(
                    tool_name=tool_call["name"],
                    args=tool_call.get("args", {}),
                    context=log_ctx,
                    verdict=str(verdict),
                )
                # _enforce는 verdict != ALLOW일 때 항상 예외를 던진다.
                _enforce(verdict, rule_id, rationale, evt_id=event["evt"])

        # ② live-rerun 위치 매칭: 실제(부작용 있는) 함수 호출보다 먼저,
        #    녹화된 시퀀스의 같은 자리인지 확인한다(§6 인자 매칭 규칙).
        if self._replay_engine is not None:
            self._replay_engine.match(tool_call["name"], tool_call.get("args", {}))

        # ③ 통과한 경우에만 기록. §6 매칭 키 seq는 EventStore 내부에서 부여.
        return self._event_store.record_tool_wrap(
            tool_name=tool_call["name"],
            args=tool_call.get("args", {}),
            context=log_ctx,
            verdict="allow",
        )

    def _intercept(
        self,
        tool_call: dict[str, Any],
        do_call: Callable[[], Any],
        stage_ctx: dict[str, Any] | None,
        log_ctx: dict[str, Any] | None,
    ) -> Any:
        """집행 표면(§3 표, 권장, 강제 집행 경로) — 동기 도구 전용.

        검사·기록은 `_run_guardrail_stages`에 위임하고, 여기서는 실제
        도구 호출(동기)과 outcome 기록만 담당한다. mode="live-rerun"
        위치 매칭도 `_run_guardrail_stages` 안에서 수행된다(§6).

        Args:
            tool_call: {"name": str, "args": dict} 형태의 호출 정보.
            do_call: 실제 도구 함수를 호출하는 no-arg callable.
            stage_ctx: §5 세션 누적 상태. stage 함수에 그대로 전달.
            log_ctx: §9 정적 메타데이터. record_tool_wrap에 그대로 전달.

        Raises:
            Denied | RetryRequested | ApprovalRequired: 첫 non-allow 판정.
            ReplayMismatchError: live-rerun 위치 매칭 실패.
        """
        event = self._run_guardrail_stages(tool_call, stage_ctx, log_ctx)
        try:
            result = do_call()
        except Exception as exc:
            self._event_store.record_error(event, exc, severity=SEVERITY_WARNING)
            raise
        else:
            self._event_store.record_ok(event)
            return result
```

- [ ] **Step 3: 리팩터 후 회귀 확인**

Run: `.venv/bin/pytest -q`
Expected: Step 1과 동일한 개수로 전체 통과. 하나라도 실패하면 `_run_guardrail_stages` 추출 과정에서 로직이 바뀐 것이므로 원본 `_intercept`와 한 줄씩 대조해 고친다.

- [ ] **Step 4: Commit**

```bash
git add src/rein/harness.py
git commit -m "refactor: _intercept에서 _run_guardrail_stages 헬퍼 추출 (동작 변경 없음)"
```

---

### Task 2: `ConcurrentToolCallError` + async 상태 필드 추가

**Files:**
- Modify: `src/rein/harness.py:23-28` (import 블록)
- Modify: `src/rein/harness.py:60-73` (`_enforce` 아래에 새 예외 클래스)
- Modify: `src/rein/harness.py:169-174` (`Harness.__init__`)
- Test: `tests/test_harness_async.py` (신규 파일 — 이 태스크에서 첫 테스트 하나만 추가)

**Interfaces:**
- Produces: `rein.harness.ConcurrentToolCallError(RuntimeError)`, `Harness._async_lock: asyncio.Lock`, `Harness._in_flight_tool: str | None`.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_harness_async.py` 새로 생성:

```python
"""M4 #78: register_tool 비동기(async def) 도구 지원 테스트.

신규 의존성(pytest-asyncio) 추가 없이 asyncio.run()으로 감싼 일반
def test_* 함수로 작성한다(설계 스펙
docs/superpowers/specs/2026-08-21-async-tool-support-design.md 참고).
"""

from rein.harness import Harness


def test_harness_construct_without_event_loop(tmp_path):
    """이벤트 루프 밖에서 생성해도 asyncio.Lock() eager 생성이 에러 없이 통과하는지 확인"""
    h = Harness(record=tmp_path / "run.jsonl")
    assert h._async_lock is not None
    assert h._in_flight_tool is None
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/pytest tests/test_harness_async.py -v`
Expected: FAIL — `AttributeError: 'Harness' object has no attribute '_async_lock'`

- [ ] **Step 3: import 추가**

`src/rein/harness.py`의 import 블록(23~28행)을 아래로 교체:

```python
import asyncio
import functools
import inspect
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypeVar
```

- [ ] **Step 4: `ConcurrentToolCallError` 클래스 추가**

`src/rein/harness.py`에서 `_enforce` 함수 정의(60~73행) 바로 뒤, `_snapshot_context_for_log` 함수 정의 앞에 삽입:

```python
class ConcurrentToolCallError(RuntimeError):
    """asyncio.gather 등으로 등록된 도구를 실제 동시에 호출하면 발생한다(M4 #78).

    rein은 순차 async만 지원한다 — §6 위치 기반 리플레이 매칭이 "seq 부여
    순서 = 호출 순서"라는 불변식에 의존하기 때문에, 진짜 병렬 실행은
    조용히 직렬화하지 않고 fail-closed로 거부한다.
    """
```

- [ ] **Step 5: `Harness.__init__`에 async 상태 필드 추가**

`src/rein/harness.py`의 `__init__` 안, 다음 블록:

```python
        # live-rerun: 실제 함수 호출 직전 위치 매칭(§6)에 쓸 엔진.
        # record 모드에서는 None으로 두어 _intercept가 match()를 건너뛴다.
        self._replay_engine: ReplayEngine | None = None
        if mode == "live-rerun":
            self._replay_engine = ReplayEngine(self.replay_from, mode="live-rerun")

        # §5 fail-closed: 구조(YAML 파싱/타입) 검증은 생성 시점에 즉시 한다.
```

를 아래로 교체(중간에 두 줄 삽입):

```python
        # live-rerun: 실제 함수 호출 직전 위치 매칭(§6)에 쓸 엔진.
        # record 모드에서는 None으로 두어 _intercept가 match()를 건너뛴다.
        self._replay_engine: ReplayEngine | None = None
        if mode == "live-rerun":
            self._replay_engine = ReplayEngine(self.replay_from, mode="live-rerun")

        # M4 #78: 순차 async 지원. 락 구간 안에서 정확히 하나의 tool_wrap만
        # 진행되도록 강제해 §6 위치 기반 매칭을 sync와 동일하게 보존한다.
        # Python 3.11+에서는 실행 중인 이벤트 루프 없이 생성해도 안전하다.
        self._async_lock = asyncio.Lock()
        self._in_flight_tool: str | None = None

        # §5 fail-closed: 구조(YAML 파싱/타입) 검증은 생성 시점에 즉시 한다.
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_harness_async.py -v`
Expected: PASS

- [ ] **Step 7: 전체 스위트 회귀 확인**

Run: `.venv/bin/pytest -q`
Expected: 전체 통과(Task 1 종료 시점과 동일 + 새 테스트 1건 추가).

- [ ] **Step 8: Commit**

```bash
git add src/rein/harness.py tests/test_harness_async.py
git commit -m "feat: ConcurrentToolCallError + Harness async 상태 필드 추가"
```

---

### Task 3: `register_tool` async 분기 + `_intercept_async` 구현 (핵심 기능)

**Files:**
- Modify: `src/rein/harness.py:203-251` (`register_tool` 메서드)
- Modify: `src/rein/harness.py` (Task 1에서 만든 `_intercept` 메서드 바로 뒤에 `_intercept_async` 추가)
- Test: `tests/test_harness_async.py`에 테스트 추가

**Interfaces:**
- Consumes: `Harness._run_guardrail_stages`(Task 1), `Harness._async_lock`/`_in_flight_tool`(Task 2), `ConcurrentToolCallError`(Task 2).
- Produces: `Harness._intercept_async(tool_call: dict, do_call: Callable[[], Awaitable[Any]], stage_ctx, log_ctx) -> Any` (코루틴). `register_tool`이 `async def` 함수를 받으면 코루틴 함수를 반환.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_harness_async.py`에 추가:

```python
def test_async_tool_basic_call(tmp_path):
    """async 도구 등록·호출이 정상 동작하고 tool_wrap+outcome이 기록되는지 확인"""
    import asyncio
    import json

    log_path = tmp_path / "run.jsonl"
    h = Harness(record=log_path)

    @h.register_tool
    async def add(a: int, b: int) -> int:
        return a + b

    result = asyncio.run(add(2, 3))
    assert result == 5

    h._event_store.close()
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["source"] == "tool_wrap"
    assert lines[0]["verdict"] == "allow"
    assert lines[1]["source"] == "outcome"
    assert lines[1]["outcome"]["status"] == "ok"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/pytest tests/test_harness_async.py::test_async_tool_basic_call -v`
Expected: FAIL — `TypeError: M1은 동기 함수만 지원합니다` (아직 register_tool이 async를 거부하는 상태)

- [ ] **Step 3: `register_tool` async 분기 구현**

`src/rein/harness.py`의 `register_tool` 메서드(203~251행) 전체를 아래로 교체:

```python
    def register_tool(self, func: F) -> F:
        """도구 정의에 붙이는 데코레이터. 인터셉터의 단일 길목을 통과시킨다.

        M4 #78: async def도 지원한다 — 단, 순차 실행만(한 번에 하나씩
        await). asyncio.gather 등으로 실제 동시 호출되면
        ConcurrentToolCallError로 거부된다(상세 근거는
        docs/superpowers/specs/2026-08-21-async-tool-support-design.md).
        """
        is_async = inspect.iscoroutinefunction(func)

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

        if is_async:

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                stage_ctx = self._session_state
                log_ctx = _snapshot_context_for_log(self._context)
                bound = _bound_args(args, kwargs)
                tool_call = {"name": func.__name__, "args": bound}
                return await self._intercept_async(
                    tool_call, lambda: func(*args, **kwargs), stage_ctx, log_ctx
                )

            return async_wrapper  # type: ignore

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # [이슈 #65] §5/§9 분리. 두 객체는 서로 다른 dict다.
            stage_ctx = self._session_state
            log_ctx = _snapshot_context_for_log(self._context)

            bound = _bound_args(args, kwargs)
            tool_call = {"name": func.__name__, "args": bound}

            # 검사 + 실행 진행
            return self._intercept(tool_call, lambda: func(*args, **kwargs), stage_ctx, log_ctx)

        return wrapper  # type: ignore
```

- [ ] **Step 4: `_intercept_async` 메서드 추가**

`src/rein/harness.py`에서 (Task 1로 축소된) `_intercept` 메서드 정의 바로 뒤, `_observe` 메서드 앞에 삽입:

```python
    async def _intercept_async(
        self,
        tool_call: dict[str, Any],
        do_call: Callable[[], Any],
        stage_ctx: dict[str, Any] | None,
        log_ctx: dict[str, Any] | None,
    ) -> Any:
        """집행 표면의 async 버전(M4 #78) — 순차 async 전용.

        `asyncio.gather` 등으로 실제 동시 호출되면 즉시
        `ConcurrentToolCallError`를 던져 거부한다(조용한 직렬화 금지).
        락 획득부터 해제까지 구간에서 `await`는 `await do_call()` 한
        곳뿐이라, "검사 시작 → seq 부여 → 실행 → outcome 기록"이
        원자적 단위가 되고 seq 부여 순서가 항상 호출 순서와 일치한다
        (sync 모드와 동일한 §6 보장).

        Args:
            tool_call: {"name": str, "args": dict} 형태의 호출 정보.
            do_call: 실제 도구 코루틴을 반환하는 no-arg callable.
            stage_ctx: §5 세션 누적 상태.
            log_ctx: §9 정적 메타데이터.

        Raises:
            ConcurrentToolCallError: 다른 async 도구 호출이 이미 진행 중.
            Denied | RetryRequested | ApprovalRequired: 첫 non-allow 판정.
            ReplayMismatchError: live-rerun 위치 매칭 실패.
        """
        if self._async_lock.locked():
            raise ConcurrentToolCallError(
                f"'{tool_call['name']}' 호출이 이미 진행 중인 "
                f"'{self._in_flight_tool}' 호출과 겹쳤습니다. rein은 순차 "
                "async만 지원합니다 — asyncio.gather로 등록된 도구를 묶지 "
                "말고 하나씩 await 하세요."
            )
        async with self._async_lock:
            self._in_flight_tool = tool_call["name"]
            try:
                event = self._run_guardrail_stages(tool_call, stage_ctx, log_ctx)
                try:
                    result = await do_call()
                except Exception as exc:
                    self._event_store.record_error(event, exc, severity=SEVERITY_WARNING)
                    raise
                else:
                    self._event_store.record_ok(event)
                    return result
            finally:
                self._in_flight_tool = None
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv/bin/pytest tests/test_harness_async.py -v`
Expected: PASS (2건 — Task 2, 3에서 추가한 테스트 모두)

- [ ] **Step 6: 전체 스위트 확인**

Run: `.venv/bin/pytest -q`
Expected: 아직 `test_register_tool_rejects_async`류 3건은 실패한다(Task 6에서 정리 예정) — 그 외 전부 통과해야 한다. 다른 실패가 있으면 이번 변경이 원인인지 확인해 고친다.

- [ ] **Step 7: Commit**

```bash
git add src/rein/harness.py tests/test_harness_async.py
git commit -m "feat: register_tool 비동기(async def) 도구 지원 (#78)"
```

---

### Task 4: 에러 경로 검증 — 예외 기록, deny 시 락 해제, 동시 호출 거부

**Files:**
- Modify: `tests/test_harness_async.py` (테스트 추가만, 프로덕션 코드 변경 없음 — Task 3 구현이 이미 이 동작들을 만족하는지 검증)

**Interfaces:**
- Consumes: `Harness._intercept_async`(Task 3), `ConcurrentToolCallError`(Task 2).

- [ ] **Step 1: 세 테스트 작성**

`tests/test_harness_async.py`에 추가:

```python
def test_async_tool_exception_records_error(tmp_path):
    """도구가 예외를 던지면 outcome error가 기록되고 예외가 그대로 전파되는지 확인"""
    import asyncio
    import json

    import pytest

    log_path = tmp_path / "run.jsonl"
    h = Harness(record=log_path)

    @h.register_tool
    async def boom():
        raise ValueError("망함")

    async def run():
        with pytest.raises(ValueError, match="망함"):
            await boom()

    asyncio.run(run())

    h._event_store.close()
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    outcome = lines[1]
    assert outcome["source"] == "outcome"
    assert outcome["outcome"]["status"] == "error"
    assert outcome["outcome"]["severity"] == "warning"


def test_async_tool_denied_releases_lock(tmp_path):
    """deny된 async 호출 후에도 락이 정상 해제돼 다음 호출이 막히지 않는지 확인"""
    import asyncio

    import pytest

    from rein.guardrails.exceptions import Denied
    from rein.guardrails.verdict import Verdict

    h = Harness(record=tmp_path / "run.jsonl")

    def deny_stage(tool_call, ctx):
        return Verdict.DENY, "rule_block", "차단", "evt_x"

    h.register_stage("budget", deny_stage)

    @h.register_tool
    async def blocked_tool():
        return "실행되면 안 됨"

    async def run():
        with pytest.raises(Denied):
            await blocked_tool()
        # 락이 정상 해제됐다면 두 번째 호출도 (ConcurrentToolCallError 없이)
        # 똑같이 Denied를 던져야 한다.
        with pytest.raises(Denied):
            await blocked_tool()

    asyncio.run(run())
    assert h._in_flight_tool is None


def test_async_concurrent_gather_rejected(tmp_path):
    """asyncio.gather로 실제 동시 호출하면 하나는 성공, 하나는 ConcurrentToolCallError"""
    import asyncio

    from rein.harness import ConcurrentToolCallError

    h = Harness(record=tmp_path / "run.jsonl")

    @h.register_tool
    async def slow_tool():
        await asyncio.sleep(0.05)
        return "done"

    async def run():
        return await asyncio.gather(slow_tool(), slow_tool(), return_exceptions=True)

    results = asyncio.run(run())
    successes = [r for r in results if r == "done"]
    errors = [r for r in results if isinstance(r, ConcurrentToolCallError)]
    assert len(successes) == 1
    assert len(errors) == 1
```

- [ ] **Step 2: 테스트 실행**

Run: `.venv/bin/pytest tests/test_harness_async.py -v`
Expected: PASS 전체 (Task 3 구현이 이미 이 세 가지를 만족해야 한다). 만약 `test_async_tool_denied_releases_lock`이 실패하면 `_intercept_async`의 `finally` 블록이 `_run_guardrail_stages`의 예외 경로에서도 실행되는지 확인한다(구조상 `async with` 블록 안에서 예외가 나므로 `finally`가 항상 실행돼야 한다). `test_async_concurrent_gather_rejected`가 flaky하면 `asyncio.sleep(0.05)`를 늘려 재현성을 높인다.

- [ ] **Step 3: Commit**

```bash
git add tests/test_harness_async.py
git commit -m "test: async 도구 에러 경로(예외 기록/deny 락 해제/동시 호출 거부) 검증"
```

---

### Task 5: 순서 보장 검증 — 순차 seq 일치, sync/async 혼용, replay-verify 라운드트립

**Files:**
- Modify: `tests/test_harness_async.py` (테스트 추가만, 프로덕션 코드 변경 없음)

**Interfaces:**
- Consumes: `Harness._intercept_async`(Task 3), `rein.replay.engine.ReplayEngine`(무변경).

- [ ] **Step 1: 세 테스트 작성**

`tests/test_harness_async.py`에 추가:

```python
def test_async_sequential_seq_matches_call_order(tmp_path):
    """두 async 도구를 순서대로 await하면 seq가 호출 순서(0,1)와 일치하는지 확인"""
    import asyncio
    import json

    log_path = tmp_path / "run.jsonl"
    h = Harness(record=log_path)

    @h.register_tool
    async def echo(x: int) -> int:
        return x

    async def run():
        await echo(1)
        await echo(2)

    asyncio.run(run())
    h._event_store.close()

    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    tool_wraps = [line for line in lines if line["source"] == "tool_wrap"]
    assert [tw["seq"] for tw in tool_wraps] == [0, 1]
    assert [tw["args"]["x"] for tw in tool_wraps] == [1, 2]


def test_mixed_sync_async_tools_ordering(tmp_path):
    """한 Harness에 sync/async 도구를 섞어 등록해도 순차 호출 시 seq 순서가 정상인지 확인"""
    import asyncio
    import json

    log_path = tmp_path / "run.jsonl"
    h = Harness(record=log_path)

    @h.register_tool
    def sync_tool(x: int) -> int:
        return x

    @h.register_tool
    async def async_tool(x: int) -> int:
        return x

    async def run():
        sync_tool(1)
        await async_tool(2)
        sync_tool(3)

    asyncio.run(run())
    h._event_store.close()

    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    tool_wraps = [line for line in lines if line["source"] == "tool_wrap"]
    assert [tw["seq"] for tw in tool_wraps] == [0, 1, 2]
    assert [tw["args"]["x"] for tw in tool_wraps] == [1, 2, 3]


def test_async_replay_verify_roundtrip(tmp_path):
    """async Harness로 기록한 run.jsonl을 기존 ReplayEngine(무변경)으로 그대로 재생"""
    import asyncio

    from rein.replay.engine import ReplayEngine

    log_path = tmp_path / "run.jsonl"
    h = Harness(record=log_path)

    @h.register_tool
    async def fetch(url: str) -> str:
        return f"fetched:{url}"

    async def run():
        await fetch("https://a.example")
        await fetch("https://b.example")

    asyncio.run(run())
    h._event_store.close()

    engine = ReplayEngine(log_path, mode="replay-verify")
    assert len(engine) == 2
    first = engine.match("fetch", {"url": "https://a.example"})
    assert first["seq"] == 0
    second = engine.match("fetch", {"url": "https://b.example"})
    assert second["seq"] == 1
```

- [ ] **Step 2: 테스트 실행**

Run: `.venv/bin/pytest tests/test_harness_async.py -v`
Expected: PASS 전체(8건).

- [ ] **Step 3: Commit**

```bash
git add tests/test_harness_async.py
git commit -m "test: async 도구 순서 보장(seq 일치/sync 혼용/replay-verify) 검증"
```

---

### Task 6: 구식 "async 거부" 테스트 정리 (3개 파일)

**Files:**
- Modify: `tests/test_harness_issue_4.py:44-50`
- Modify: `tests/test_pipeline.py:22-30`
- Modify: `tests/test_smoke.py:20-25`

**Interfaces:**
- Consumes: `Harness.register_tool`의 새 async 동작(Task 3).

- [ ] **Step 1: `tests/test_harness_issue_4.py` 수정**

기존:

```python
def test_register_tool_rejects_async(harness):
    """비동기(async) 함수 등록 시 명세대로 TypeError를 뱉는지 확인"""
    with pytest.raises(TypeError, match="M1은 동기 함수만 지원합니다"):

        @harness.register_tool
        async def async_tool():
            pass
```

교체:

```python
def test_register_tool_accepts_async(harness):
    """M4 #78: 비동기(async) 함수도 도구로 등록·실행할 수 있는지 확인"""
    import asyncio

    @harness.register_tool
    async def async_add(a: int, b: int) -> int:
        return a + b

    result = asyncio.run(async_add(2, 3))
    assert result == 5
```

- [ ] **Step 2: `tests/test_pipeline.py` 수정**

기존:

```python
def test_async_tool_blocked():
    """M1 명세에 따라 비동기 함수 등록 시 즉시 TypeError를 던지는지 확인"""
    h = Harness(record="dummy.jsonl")

    with pytest.raises(TypeError, match="M1은 동기 함수만 지원합니다"):

        @h.register_tool
        async def async_dummy():
            pass
```

교체:

```python
def test_async_tool_allowed():
    """M4 #78: 비동기 함수 등록이 더 이상 TypeError 없이 정상 실행되는지 확인"""
    import asyncio

    h = Harness(record="dummy.jsonl")

    @h.register_tool
    async def async_dummy(x, y):
        return x + y

    result = asyncio.run(async_dummy(3, 4))
    assert result == 7
```

- [ ] **Step 3: `tests/test_smoke.py` 수정**

기존:

```python
def test_register_tool_rejects_async():
    h = Harness(record="dummy.jsonl")
    with pytest.raises(TypeError, match="M1은 동기 함수만 지원합니다"):

        @h.register_tool
        async def async_tool(): ...
```

교체:

```python
def test_register_tool_accepts_async():
    import asyncio

    h = Harness(record="dummy.jsonl")

    @h.register_tool
    async def async_tool(): ...

    asyncio.run(async_tool())
```

- [ ] **Step 4: 전체 스위트 통과 확인**

Run: `.venv/bin/pytest -q`
Expected: 전체 통과, 실패 0건.

- [ ] **Step 5: lint 확인**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check .`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add tests/test_harness_issue_4.py tests/test_pipeline.py tests/test_smoke.py
git commit -m "test: M1 async 거부 테스트를 async 허용 테스트로 교체 (#78)"
```

---

### Task 7: CLAUDE.md §4 문서 갱신 (living file)

**Files:**
- Modify: `CLAUDE.md` (§4 "M1 스코프 제약 — 동기 호출만 지원" 문단)

**Interfaces:** 없음(문서 전용 변경).

- [ ] **Step 1: 기존 문단 확인**

CLAUDE.md §4에서 다음 문단을 찾는다(정확한 텍스트):

```markdown
**M1 스코프 제약 — 동기 호출만 지원**: `register_tool`은 `async def`를
거부한다.
```python
if inspect.iscoroutinefunction(func):
    raise TypeError("M1은 동기 함수만 지원합니다")
```
동시 호출이 record와 replay-verify 사이에서 완료 순서가 달라지면 §6의
위치 기반 매칭이 깨지기 때문에, "동시 호출을 감지해서 처리"하는 대신
**애초에 등록을 막아 문제 자체를 스코프 아웃**한다. 비동기 지원은 M4
이후 검토 대상이다.
```

- [ ] **Step 2: 구현 완료 사실로 교체**

위 문단을 아래로 교체:

```markdown
**비동기(async) 도구 지원 (M4, 이슈 #78 완료)**: `register_tool`은
`inspect.iscoroutinefunction(func)`로 분기해 `async def` 도구도
등록·실행한다. 단 **순차 async만 지원**한다 — 에이전트 루프가 매
호출을 `await`로 하나씩 순서대로 처리하는 패턴만 대상이며, 도구의
진짜 병렬 실행(`asyncio.gather`로 여러 등록 도구를 실제 동시에
호출하는 것)은 지원하지 않는다.

`Harness`는 인스턴스당 `asyncio.Lock`을 하나 갖고, 도구 하나의
"검사 → seq 부여 → 실행 → outcome 기록" 구간 전체를 이 락으로
묶는다. 락 보유 중 `await`가 걸리는 지점은 실제 도구 호출
(`await do_call()`) 한 곳뿐이라 — 가드레일 스테이지 순회·이벤트
기록은 전부 동기 코드라 이벤트 루프를 양보하지 않는다 — seq 부여
순서가 항상 호출 순서와 일치한다. `asyncio.gather` 등으로 두 번째
호출이 락이 잡힌 상태에서 들어오면 조용히 대기시키지 않고 즉시
`ConcurrentToolCallError`를 던져 거부한다(§5의 "조용한 무시 금지"
원칙과 동일한 이유 — 사용자가 의도치 않게 성능만 잃고 아무 신호를
못 받는 상태를 막는다).

이 설계 덕분에 `ReplayEngine`/`EventStore`/§9 이벤트 스키마는 무변경
그대로다 — "seq 부여 순서 = 호출 순서"라는 sync 모드의 불변식이
async에서도 100% 유지되기 때문에 §6 위치 기반 리플레이 매칭이 그대로
적용된다. sync 도구 호출은 `await`가 없는 일반 함수 호출이라 그
자체로 이벤트 루프를 블로킹하므로, 한 Harness에 sync/async 도구를
섞어 등록해도 별도 조정 로직 없이 순서가 보장된다.

`observe_model`의 비동기 모델 클라이언트(AsyncOpenAI/AsyncAnthropic)
자동 배선과 `register_stage` 커스텀 가드레일 스테이지의 `async def`
지원은 별개 이슈다(이번 스코프 아님). 설계 근거 전문은
`docs/superpowers/specs/2026-08-21-async-tool-support-design.md`
참고.
```

- [ ] **Step 3: living file 원칙 확인**

CLAUDE.md 파일 전체를 검색해 "M1은 동기 함수만 지원" 또는 "동기 호출만 지원"을 언급하는 다른 문단이 남아있지 않은지 확인한다:

Run: `grep -n "동기 함수만 지원\|동기 호출만 지원" CLAUDE.md`
Expected: 결과 없음(0건) — 있다면 그 문단도 같은 방식으로 갱신한다.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md §4 비동기 도구 지원 완료 반영 (#78)"
```

---

## Self-Review 결과 (계획 작성자 기록)

- **스펙 커버리지**: 설계 스펙의 "결정한 것" 1~5번 전부 Task 3(핵심 구현)에서 구현되고 Task 4/5(각각 에러 경로/순서 보장)에서 검증됨. "테스트 계획" 8건 전부 Task 2(1건)·4(3건)·5(3건)·2(나머지 1건, `test_harness_construct_without_event_loop`)에 정확히 매핑됨. "영향받는 기존 테스트" 3건은 Task 6에서 처리. CLAUDE.md 갱신은 Task 7.
- **플레이스홀더 스캔**: "TBD/TODO/나중에 구현" 없음. 모든 스텝에 실제 코드 포함.
- **타입/시그니처 일관성**: `_run_guardrail_stages(tool_call, stage_ctx, log_ctx) -> dict[str, Any]`가 Task 1에서 정의된 그대로 Task 3의 `_intercept_async`에서 동일 시그니처로 호출됨. `ConcurrentToolCallError`는 Task 2에서 정의, Task 3(구현)·Task 4(테스트)에서 동일 이름으로 사용됨.

---

**Plan complete and saved to `docs/superpowers/plans/2026-08-21-async-tool-support.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - 태스크마다 새 subagent를 붙여 2단계 리뷰까지 진행

**2. Inline Execution** - 이 세션에서 executing-plans로 배치 실행, 체크포인트마다 확인

**Which approach?**
