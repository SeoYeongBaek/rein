# M4 설계: register_tool 비동기(async def) 도구 지원 (#78)

날짜: 2026-08-21
작성: 서영 (브레인스토밍 세션)
관련 마일스톤: CLAUDE.md §12 M4, 이슈 #78

## 배경

M3까지의 모든 이슈(#1~#80)가 closed됐고, M4 확장 버킷 중 "추가
어댑터"(#80, 최우선)가 이미 구현·머지됐다. 다음 착수 대상으로 #78을
정했다.

M1은 `register_tool`이 `async def`를 받으면 즉시 `TypeError`를 던져
비동기 도구 등록 자체를 스코프 아웃했다(`harness.py::register_tool`).
근거는 CLAUDE.md §4/§6에 명시돼 있다.

> §4: "동시 호출이 record와 replay-verify 사이에서 완료 순서가
> 달라지면 §6의 위치 기반 매칭이 깨지기 때문에, '동시 호출을 감지해서
> 처리'하는 대신 애초에 등록을 막아 문제 자체를 스코프 아웃한다."
>
> §6: "리플레이 시 LLM을 다시 호출하지 않고... n번째 인터셉트 호출은
> 무조건 로그의 n번째 tool_wrap 이벤트에 대응시킨다. 위치(시퀀스
> 인덱스) 기반 매칭이 원칙이다."

`register_tool`의 실제 구현(`harness.py`)을 확인한 결과, `_intercept`는
완전히 동기 함수이고, `EventStore`는 `threading.Lock`으로 개별
`record_*` 호출의 스레드 안전만 보장한다(호출 순서 자체의 결정론은
"한 프로세스가 순서대로 도구를 부른다"는 M1의 암묵적 전제에 기대고
있었다). `ReplayEngine.match()`도 순수 position 기반이며, seq는
`EventStore._seq`가 `record_tool_wrap` 호출 순서로 단조 증가시킨다.

## 스코프

- **순차 async만 지원한다.** `asyncio.gather()` 등으로 등록된 도구를
  "진짜 동시에" 여러 개 실행하는 것은 지원하지 않는다(아래 "결정한
  것" 1번). 에이전트 루프가 매 호출을 `await`로 하나씩 순서대로
  처리하는 사용 패턴만 대상이다.
- `register_tool`/`_intercept` 경로만 다룬다. `observe_model`의
  비동기 모델 클라이언트(AsyncOpenAI/AsyncAnthropic) 자동 배선과
  `register_stage` 커스텀 가드레일 스테이지의 async 지원은 이번
  스펙 범위 밖이다(둘 다 별도 이슈로 분리).

## 결정한 것

### 1. 동시 호출은 조용히 직렬화하지 않고 즉시 에러로 거부한다 (fail-closed)

`asyncio.gather(tool_a(), tool_b())`처럼 등록된 도구 여러 개를 실제로
동시에 호출하면, 락으로 조용히 대기시켜 순서대로 처리하는 대신
**`ConcurrentToolCallError`를 즉시 던진다.** 조용한 직렬화는 사용자가
의도치 않게 성능을 잃으면서도 아무 신호를 못 받는 상태를 만든다 —
§5의 "조용한 무시 금지" 원칙과 같은 이유로, 동시 호출 시도 자체를
명시적으로 드러낸다.

### 2. `Harness._async_lock` + `_intercept_async`로 위치 결정론을 보존한다

```python
class ConcurrentToolCallError(RuntimeError):
    """asyncio.gather 등으로 등록된 도구를 실제 동시에 호출하면 발생.
    rein은 순차 async만 지원한다(§6 위치 기반 리플레이 매칭 보존)."""


class Harness:
    def __init__(self, ...):
        ...
        self._async_lock = asyncio.Lock()  # Python 3.11+: 실행 중인 루프 불필요
        self._in_flight_tool: str | None = None

    async def _intercept_async(self, tool_call, do_call, stage_ctx, log_ctx):
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

`_run_guardrail_stages(tool_call, stage_ctx, log_ctx)`는 기존
`_intercept`의 "① 검사(첫 non-allow 승리) → non-allow면 tool_wrap
기록 후 예외 → ② live-rerun 위치 매칭 → ③ 통과 시 tool_wrap 기록"
부분을 추출한 공유 헬퍼다. `_intercept`(sync)와 `_intercept_async`
양쪽이 호출하며, `do_call()` 실행(sync 직접 호출 vs `await`)과 그
전후의 outcome 기록만 두 메서드가 각자 담당한다.

**핵심 불변식**: 락 획득부터 해제까지 구간에서 `await`는
`await do_call()` 한 곳뿐이다. 가드레일 스테이지 순회·
`record_tool_wrap`·`record_ok`/`record_error`는 전부 동기 코드라
이벤트 루프를 양보하지 않는다. 따라서 "체크 시작 → seq 부여 → 실행 →
outcome 기록"이 원자적 단위가 되고, seq 부여 순서는 언제나 호출
순서와 일치한다 — sync 모드와 동일한 보장이다.

**동시 진입 감지에 race가 없는 이유**: `lock.locked()` 체크와
`async with self._async_lock:` 진입 사이에 `await`가 없다. 락이
비어있을 때 `asyncio.Lock.acquire()`는 실제로 suspend하지 않고 즉시
반환하므로(내부적으로 대기 큐를 거치지 않음), 단일 이벤트 루프
안에서는 check-then-acquire가 원자적이다. (멀티스레드로 별도
이벤트 루프에서 같은 Harness 인스턴스를 동시에 건드리는 경우는
스코프 밖 — sync-only 시절에도 다루지 않던 문제라 이번에도 명시적
비보장으로 남긴다.)

**non-allow(deny/retry/approve) 경로도 락을 정상 해제한다**:
`_run_guardrail_stages`가 예외를 던지는 지점이 `async with` 블록
안이므로 `finally`가 `_in_flight_tool`을 정리하고 락도 해제된다.
deny된 호출 뒤에 락이 영구 점유되는 버그가 없다.

### 3. sync/async 도구는 추가 조정 로직 없이 한 Harness에 섞어 쓸 수 있다

sync 도구 호출(`_intercept`)은 `await`가 없는 일반 함수 호출이라
그 자체로 이벤트 루프를 블로킹한다 — 실행되는 동안 다른 코루틴(async
도구 호출 포함)은 절대 끼어들 수 없다. 그래서 `_async_lock`은
async 경로끼리의 동시 진입만 방어하면 충분하고, sync↔async 사이의
상호배제는 파이썬 코루틴 스케줄링 모델 자체가 공짜로 보장한다.

### 4. `register_tool`은 `inspect.iscoroutinefunction`으로 분기하되 공개 API는 그대로 하나다

```python
def register_tool(self, func: F) -> F:
    self._activate()
    is_async = inspect.iscoroutinefunction(func)
    # ... _bound_args/tool_call 구성은 기존과 동일, sync/async 공유 ...
    if is_async:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            stage_ctx = self._session_state
            log_ctx = _snapshot_context_for_log(self._context)
            bound = _bound_args(args, kwargs)
            tool_call = {"name": func.__name__, "args": bound}
            return await self._intercept_async(
                tool_call, lambda: func(*args, **kwargs), stage_ctx, log_ctx
            )
        return wrapper
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        ...  # 기존 sync 경로, 변경 없음
    return wrapper
```

M1의 `TypeError("M1은 동기 함수만 지원합니다")` 분기는 제거한다. 별도
공개 메서드(`register_async_tool` 등)를 신설하는 대안은 §4가 이미
동결한 "5줄 통합" API 표면(`register_tool` 단일 진입점)을 넓히므로
채택하지 않는다 — `inspect.iscoroutinefunction` 분기는 이미 M1의
TypeError 체크가 쓰던 방식을 그대로 확장한 것뿐이라 기존 관례와도
일관된다.

### 5. `ReplayEngine`/`EventStore`/§9 스키마는 한 줄도 바뀌지 않는다

순차 실행 + fail-closed 거부 조합 덕분에, 기록되는 이벤트 시퀀스는
"항상 정확히 하나의 tool_wrap이 진행 중"이라는 sync 모드의 불변식을
그대로 유지한다. `rein replay`/`rein rule-from`/`rein report` 등
기존 CLI와 리플레이 엔진은 async로 기록된 로그를 아무 변경 없이
그대로 소비할 수 있다.

## 테스트 계획

기존 의존성에 `pytest-asyncio`가 없고(Python `>=3.11` 요구사항만
있음), 새 의존성을 추가하지 않기 위해 `asyncio.run()`으로 감싸는
일반 `def test_*` 함수로 작성한다(§10 "자체 구현은 얇게").

**기존 테스트 영향** — 다음 세 곳이 async 등록 시 `TypeError`를
검증하고 있어 구현 시 제거/교체 필요:
`tests/test_harness_issue_4.py:46`, `tests/test_pipeline.py:26`,
`tests/test_smoke.py:22`.

**신규 테스트**

| 테스트 | 검증 내용 |
|---|---|
| `test_async_tool_basic_call` | async 도구 등록·호출 정상 동작, `run.jsonl`에 tool_wrap+outcome(ok) 기록 |
| `test_async_tool_exception_records_error` | 예외 발생 시 outcome error 기록 + 예외 그대로 전파 |
| `test_async_tool_denied_releases_lock` | deny 규칙에 걸린 호출이 `Denied`를 던진 뒤에도 락이 정상 해제돼 다음 호출이 막히지 않음 |
| `test_async_concurrent_gather_rejected` | `asyncio.gather(tool(), tool())`로 실제 동시 호출 시 하나는 정상 완료, 하나는 `ConcurrentToolCallError`(결정론적 오버랩을 위해 `asyncio.sleep`으로 첫 호출을 `await do_call()` 지점에 묶어둠) |
| `test_async_sequential_seq_matches_call_order` | 두 async 도구를 순서대로 `await`하면 `run.jsonl`의 `seq`가 호출 순서(0,1)와 일치 |
| `test_async_replay_verify_roundtrip` | async Harness로 기록한 `run.jsonl`을 기존 `ReplayEngine`/`rein replay` CLI(무변경)로 그대로 재생 |
| `test_mixed_sync_async_tools_ordering` | 한 Harness에 sync/async 도구를 섞어 등록, 순차 호출 시 seq 순서·기록 정상 |
| `test_harness_construct_without_event_loop` | 이벤트 루프 밖에서 `Harness(...)` 생성해도 에러 없음 — `asyncio.Lock()` eager 생성이 3.11에서 안전함을 회귀로 고정 |

## 스코프 밖

- `asyncio.gather` 등을 통한 도구의 진짜 병렬 실행 (§6 position 매칭
  자체를 재설계해야 하는 별개 문제 — 필요해지면 별도 이슈)
- `observe_model`의 비동기 모델 클라이언트(AsyncOpenAI/AsyncAnthropic)
  자동 배선
- `register_stage` 커스텀 가드레일 스테이지의 `async def` 지원
- 멀티스레드(여러 OS 스레드가 각자의 이벤트 루프로 같은 Harness
  인스턴스를 동시에 건드리는 상황) 안전성

## 관련 CLAUDE.md 근거

> §4: "M1 스코프 제약 — 동기 호출만 지원... 동시 호출이 record와
> replay-verify 사이에서 완료 순서가 달라지면 §6의 위치 기반 매칭이
> 깨지기 때문에... 애초에 등록을 막아 문제 자체를 스코프 아웃한다.
> 비동기 지원은 M4 이후 검토 대상이다."
>
> §6: "n번째 인터셉트 호출은 무조건 로그의 n번째 tool_wrap 이벤트에
> 대응시킨다. 인자 값은 비교하지 않는다."
>
> §12: "M4 | OSS | 확장 버킷 — ... 비동기 도구 지원 검토. 임계 경로 밖"
