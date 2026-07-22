# M3 OSS 공개 준비 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** rein을 OSS로 공개할 수 있는 상태로 만든다 — `Harness(rules=...)`가
라이브 실행에서 실제로 집행되도록 두 가지 버그를 고치고, README·LICENSE·
`examples/`를 추가한다.

**Architecture:** 기존 M1/M2 구현(인터셉터·가드레일 파이프라인·이벤트
저장소·리플레이·규칙 엔진)은 그대로 두고, `Harness._default_safety_check`가
지금까지 저장만 하고 쓰지 않던 `self.rules`를 `rein.rules.runtime`의 기존
매처(`matching_rules`/`_verdict_from_rules`/`_to_verdict`)에 연결한다.
`_intercept`는 non-allow 판정에서도 `record_tool_wrap`을 호출하도록
재구성한다. 문서/예제/라이선스는 이 두 픽스 위에 얹는다.

**Tech Stack:** Python 3.11+, pytest, ruff, PyYAML, sqlglot(간접), Typer(CLI, 변경 없음).

## Global Constraints

- 공개 API 시그니처(`Harness(record, rules, config, mode, replay_from, context)`,
  `register_tool`, `register_stage`, `observe_model`, CLI 4개 명령)는 **절대
  변경하지 않는다** — CLAUDE.md §4, M3는 "신규 설계 없음".
- 새 판정 로직·새 우선순위 매핑을 만들지 않는다 — `rein.rules.runtime`의
  기존 함수(`matching_rules`, `_verdict_from_rules`, `_to_verdict`,
  `_load_rules`)만 재사용한다(CLAUDE.md §5 "별도 VERDICT_PRIORITY 매핑
  dict는 두지 않는다"와 같은 원칙).
- `_default_safety_check`를 제외한 `schema`/`permission`/`budget`
  기본 스테이지는 손대지 않는다(여전히 ALLOW 고정 스텁).
- deny 픽스는 tool_wrap 줄만 남긴다 — outcome 줄이나 새 `outcome.status`
  값을 추가하지 않는다(§9 스키마 변경 없음).
- 모든 신규 Python 파일은 ruff 통과(`line-length=100`,
  `select=["E","F","I","UP","B"]`, `.pre-commit-config.yaml`이 전체
  파일에 대해 실행됨 — `examples/*.py`도 대상).
- `*.jsonl`/`*.html`은 `.gitignore`에 의해 이미 무시된다(단
  `demo/ab_demo/`의 두 예외는 그대로 유지) — `examples/`에서 생성되는
  실행 산출물(run*.jsonl, report.html)을 위해 별도 gitignore 수정 불필요.
- `demo/ab_demo/`(대회 A/B 데모 자산)는 이번 작업에서 건드리지 않는다.
- 스펙 문서: `docs/superpowers/specs/2026-07-21-m3-oss-release-prep-design.md`.

---

## Task 1: `Harness(rules=...)` 라이브 집행 배선 (버그 픽스 A)

**Files:**
- Modify: `src/rein/harness.py`
- Test: `tests/test_harness_rules_wiring.py` (신규)

**Interfaces:**
- Consumes: `rein.rules.runtime.matching_rules(event: dict, loaded_rules: list[dict]) -> list[dict]`,
  `rein.rules.runtime._verdict_from_rules(event: dict, loaded_rules: list[dict]) -> str`,
  `rein.rules.runtime._to_verdict(value: str) -> Verdict`,
  `rein.rules.runtime._load_rules(rules_paths: Iterable[str | Path]) -> list[dict]`
  (모두 기존 함수, 시그니처 변경 없음).
- Produces: `Harness._loaded_rules: list[dict[str, Any]]` (다른 태스크에서
  참조하지 않음, 내부 전용).

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_harness_rules_wiring.py` 전체 내용:

```python
"""Harness(rules=...) 라이브 집행 배선 테스트 (M3 스펙, harness.py 버그 픽스 A).

Harness.__init__이 저장만 하던 rules 인자를 _default_safety_check에
연결하기 전에는, rules.yaml에 매칭되는 deny 규칙이 있어도
register_tool로 계측된 도구가 실제로는 막히지 않았다. 이 테스트는 그
배선이 실제로 동작하는지 검증한다.
"""

import textwrap
from unittest.mock import patch

import pytest

from rein.guardrails.exceptions import Denied
from rein.harness import Harness

RULES_YAML = textwrap.dedent(
    """
    rule:
      id: rule_0007
      origin: auto
      when:
        tool: execute_sql
        features:
          class: { in: [DDL_DESTRUCTIVE] }
      scope:
        agent.role: content_editor
      then: deny
      rationale: "OWASP LLM06 Excessive Agency"
    """
)


@pytest.fixture
def rules_path(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(RULES_YAML, encoding="utf-8")
    return path


@pytest.fixture
def harness(tmp_path, rules_path):
    with (
        patch("rein.harness.load_stage_order", return_value=["safety"]),
        patch("rein.harness.resolve_stage_order", return_value=["safety"]),
    ):
        yield Harness(
            record=tmp_path / "run.jsonl",
            rules=str(rules_path),
            context={"agent_role": "content_editor"},
        )


def test_live_call_matching_deny_rule_raises_denied(harness):
    """rules.yaml에 매칭되는 DDL_DESTRUCTIVE + content_editor 호출은 라이브에서 deny된다."""

    @harness.register_tool
    def execute_sql(query: str) -> str:
        return f"executed: {query}"

    with pytest.raises(Denied) as exc_info:
        execute_sql(query="DROP TABLE users;")

    assert exc_info.value.rule_id == "rule_0007"


def test_live_call_not_matching_rule_still_allowed(harness):
    """같은 rules.yaml이 있어도 SQL_SAFE 쿼리는 그대로 통과한다."""

    @harness.register_tool
    def execute_sql(query: str) -> str:
        return f"executed: {query}"

    assert execute_sql(query="SELECT 1") == "executed: SELECT 1"


def test_harness_without_rules_still_allows_everything(tmp_path):
    """rules를 안 주면 기존과 동일하게 항상 allow(하위 호환)."""
    with (
        patch("rein.harness.load_stage_order", return_value=["safety"]),
        patch("rein.harness.resolve_stage_order", return_value=["safety"]),
    ):
        h = Harness(record=tmp_path / "run.jsonl")

    @h.register_tool
    def execute_sql(query: str) -> str:
        return f"executed: {query}"

    assert execute_sql(query="DROP TABLE users;") == "executed: DROP TABLE users;"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_harness_rules_wiring.py -v`
Expected: `test_live_call_matching_deny_rule_raises_denied` FAIL
(`Denied`가 발생하지 않고 `execute_sql`이 정상 리턴됨). 나머지 두 개는
이미 통과할 수 있음(현재도 allow만 반환하므로) — 그건 정상이다.

- [ ] **Step 3: `src/rein/harness.py` 수정 — import 추가**

파일 상단 import 블록(`from rein.replay import ReplayEngine` 다음 줄)에
추가:

```python
from rein.replay import ReplayEngine
from rein.rules.runtime import _load_rules, _to_verdict, _verdict_from_rules, matching_rules
```

- [ ] **Step 4: 경로 정규화 헬퍼 함수 추가**

`_snapshot_context_for_log` 함수 바로 다음(94번째 줄 `class Harness:` 앞)에
추가:

```python
def _normalize_rules_paths(rules: str | list[str] | None) -> list[str]:
    """§4 Harness(rules=...)의 str | list[str] | None을 경로 리스트로 통일."""
    if rules is None:
        return []
    if isinstance(rules, str):
        return [rules]
    return list(rules)
```

- [ ] **Step 5: `Harness.__init__`에 로딩 배선 추가**

`self.rules = rules` 바로 다음 줄에 추가(주변 코드는 변경하지 않음):

```python
        self.rules = rules
        # [버그 픽스 A] 생성 시점에 즉시 로드 — §5 fail-closed와 같은 타이밍.
        # YAML이 없거나 파싱 실패하면 Harness() 생성 시점에 바로 에러난다
        # (첫 도구 호출까지 에러를 미루지 않음).
        self._loaded_rules: list[dict[str, Any]] = _load_rules(
            _normalize_rules_paths(rules)
        )
        self.config = config
```

- [ ] **Step 6: `_default_safety_check` 구현 교체**

기존:

```python
    def _default_safety_check(
        self, tool_call: dict[str, Any], ctx: Any
    ) -> tuple[Verdict, str, str, str]:
        return Verdict.ALLOW, "", "", ""
```

교체 후:

```python
    def _default_safety_check(
        self, tool_call: dict[str, Any], ctx: Any
    ) -> tuple[Verdict, str, str, str]:
        """§5 safety 스테이지 — self._loaded_rules(rules.yaml)를 실제로
        집행한다(버그 픽스 A). 판정 로직은 새로 만들지 않고
        rein.rules.runtime의 기존 매처를 그대로 재사용한다.
        """
        if not self._loaded_rules:
            return Verdict.ALLOW, "", "", ""

        evt = {
            "tool_name": tool_call["name"],
            "args": tool_call.get("args") or {},
            "context": ctx or {},
        }
        matched = matching_rules(evt, self._loaded_rules)
        if not matched:
            return Verdict.ALLOW, "", "", ""

        verdict = _to_verdict(_verdict_from_rules(evt, self._loaded_rules))
        if verdict == Verdict.ALLOW:
            return Verdict.ALLOW, "", "", ""

        winning_rule = next(
            rule for rule in matched if _to_verdict(rule.get("then", "allow")) == verdict
        )
        return verdict, winning_rule.get("id", ""), winning_rule.get("rationale", ""), ""
```

- [ ] **Step 7: 테스트 통과 확인**

Run: `pytest tests/test_harness_rules_wiring.py -v`
Expected: 3개 테스트 모두 PASS

- [ ] **Step 8: 기존 회귀 테스트 확인**

Run: `pytest tests/ -v`
Expected: 전체 PASS (`test_intentional_fail.py`의 xfail 1건은 여전히
xfail로 표시되는 것이 정상)

- [ ] **Step 9: 커밋**

```bash
git add src/rein/harness.py tests/test_harness_rules_wiring.py
git commit -m "fix: Harness(rules=...)가 라이브 실행에서 실제로 집행되도록 배선"
```

---

## Task 2: deny/retry/approve 판정도 이벤트 저장소에 기록 (버그 픽스 B)

**Files:**
- Modify: `src/rein/harness.py` (`_intercept` 메서드)
- Test: `tests/test_harness_deny_logging.py` (신규)

**Interfaces:**
- Consumes: `EventStore.record_tool_wrap(*, tool_name, args, context, verdict) -> dict`
  (기존 메서드, 변경 없음), `GuardrailVerdictError.evt_id` 속성(기존).
- Produces: 없음(내부 동작 변경만).

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_harness_deny_logging.py` 전체 내용:

```python
"""deny 판정도 이벤트 저장소에 tool_wrap으로 기록되는지 검증
(M3 스펙, harness.py 버그 픽스 B).

픽스 전에는 _intercept가 non-allow 판정에서 즉시 예외를 던지고
return하느라 record_tool_wrap을 전혀 호출하지 않아, 막힌 호출이
JSONL에 한 줄도 남지 않았다.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from rein.guardrails.exceptions import Denied
from rein.guardrails.verdict import Verdict
from rein.harness import Harness


def _read_events(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture
def harness(tmp_path):
    with (
        patch("rein.harness.load_stage_order", return_value=["safety"]),
        patch("rein.harness.resolve_stage_order", return_value=["safety"]),
    ):
        yield Harness(record=tmp_path / "run.jsonl")


def test_denied_call_is_recorded_as_tool_wrap_with_no_outcome(harness):
    """커스텀 safety 스테이지가 deny하면 tool_wrap 줄만 기록되고 outcome은 없다."""
    harness.register_stage(
        "safety",
        lambda tool_call, ctx: (Verdict.DENY, "rule_custom", "차단 사유", "evt_placeholder"),
    )

    @harness.register_tool
    def dangerous() -> str:
        return "실행됨"

    with pytest.raises(Denied) as exc_info:
        dangerous()

    events = _read_events(harness.record_path)
    assert len(events) == 1

    tool_wrap = events[0]
    assert tool_wrap["source"] == "tool_wrap"
    assert tool_wrap["tool_name"] == "dangerous"
    assert tool_wrap["verdict"] == "deny"

    # 스테이지가 반환한 placeholder("evt_placeholder")가 아니라 방금
    # 기록된 진짜 evt로 예외가 채워진다.
    assert exc_info.value.evt_id == tool_wrap["evt"]
    assert exc_info.value.evt_id != "evt_placeholder"


def test_allowed_call_after_denied_call_still_gets_own_tool_wrap_and_outcome(harness):
    """deny 이후에도 seq/evt 카운터가 정상적으로 이어져 다음 allow 호출이 제대로 기록된다."""
    verdicts = iter([Verdict.DENY, Verdict.ALLOW])

    def flaky_safety(tool_call, ctx):
        verdict = next(verdicts)
        if verdict == Verdict.DENY:
            return Verdict.DENY, "rule_custom", "차단 사유", ""
        return Verdict.ALLOW, "", "", ""

    harness.register_stage("safety", flaky_safety)

    @harness.register_tool
    def maybe_dangerous() -> str:
        return "실행됨"

    with pytest.raises(Denied):
        maybe_dangerous()

    assert maybe_dangerous() == "실행됨"

    events = _read_events(harness.record_path)
    assert [e["source"] for e in events] == ["tool_wrap", "tool_wrap", "outcome"]
    assert events[0]["verdict"] == "deny"
    assert events[1]["verdict"] == "allow"
    assert events[2]["outcome"]["status"] == "ok"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_harness_deny_logging.py -v`
Expected: 두 테스트 모두 FAIL (`assert len(events) == 1`에서
`0 == 1`로 실패 — deny 호출이 아예 기록되지 않으므로)

- [ ] **Step 3: `_intercept` 재구성**

기존(`src/rein/harness.py`의 `_intercept` 메서드 중 판정 루프 부분):

```python
        # ① 검사: 첫 non-allow 승리(§5). stage_ctx가 stage에 직접 전달.
        for _stage_name, stage_fn in pipeline:
            verdict, rule_id, rationale, evt_id = stage_fn(tool_call, stage_ctx)
            if verdict != Verdict.ALLOW:
                # 예외로 환원 — 원본 도구는 호출되지 않음(§4).
                _enforce(verdict, rule_id, rationale, evt_id=evt_id)
                return  # type: ignore[unreachable]
```

교체 후:

```python
        # ① 검사: 첫 non-allow 승리(§5). stage_ctx가 stage에 직접 전달.
        for _stage_name, stage_fn in pipeline:
            verdict, rule_id, rationale, _stage_evt_id = stage_fn(tool_call, stage_ctx)
            if verdict != Verdict.ALLOW:
                # [버그 픽스 B] non-allow도 §9 그대로 tool_wrap 한 줄로
                # 남긴다. 실행이 없었으므로 outcome 줄은 만들지 않는다
                # (§9 생애주기 비대칭 — outcome이 없을 수 있다는 것과
                # 일관됨). evt_id는 스테이지가 반환한 placeholder 대신
                # 방금 기록된 진짜 evt를 쓴다 — 스테이지는 실제 evt id를
                # 미리 알 수 없다(부여는 EventStore의 책임).
                event = self._event_store.record_tool_wrap(
                    tool_name=tool_call["name"],
                    args=tool_call.get("args", {}),
                    context=log_ctx,
                    verdict=str(verdict),
                )
                # 예외로 환원 — 원본 도구는 호출되지 않음(§4).
                _enforce(verdict, rule_id, rationale, evt_id=event["evt"])
                return  # type: ignore[unreachable]
```

나머지(`② live-rerun 위치 매칭`, `③ 집행` 이하)는 그대로 둔다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_harness_deny_logging.py -v`
Expected: 두 테스트 모두 PASS

- [ ] **Step 5: 기존 회귀 테스트 확인**

Run: `pytest tests/ -v`
Expected: 전체 PASS. 특히 `tests/test_harness_issue_29.py`(allow 경로
verdict=="allow" 기록), `tests/test_harness_issue_33.py`,
`tests/test_harness_rules_wiring.py`(Task 1)가 여전히 통과하는지 확인.

- [ ] **Step 6: 커밋**

```bash
git add src/rein/harness.py tests/test_harness_deny_logging.py
git commit -m "fix: deny/retry/approve 판정도 tool_wrap 이벤트로 기록"
```

---

## Task 3: CLAUDE.md §4 방안 B 문구 정정 + `src/rein/__init__.py` docstring 동기화

**Files:**
- Modify: `CLAUDE.md:112-124`
- Modify: `src/rein/__init__.py:1-16`

**Interfaces:**
- Consumes: 없음(문서 전용 변경).
- Produces: 없음.

- [ ] **Step 1: `CLAUDE.md` 방안 B 예시 교체**

기존(`CLAUDE.md` 108~124번째 줄):

```
수동 `intercept(tool_name, args, ctx)` 래핑 방식은 폐기한다(모든 호출부를
손으로 감싸야 해서 "프레임워크 비종속"과 모순). 대신 두 가지 Pythonic
표면을 제공한다.

```python
# 방안 A — 도구 정의에 데코레이터 한 번 (집행 가능, 권장)
from rein import Harness
h = Harness(record="run.jsonl")

@h.register_tool  # 도구 "정의"에만 붙는다. 호출부는 그대로.
def delete_file(path: str):
    os.remove(path)

# 방안 B — 컨텍스트 매니저로 루프 전체 감싸기
with Harness(record="run.jsonl") as h:
    agent.run(task="안 쓰는 파일 정리해줘")  # 기존 코드 무수정
```
```

교체 후:

```
수동 `intercept(tool_name, args, ctx)` 래핑 방식은 폐기한다(모든 호출부를
손으로 감싸야 해서 "프레임워크 비종속"과 모순). 대신 두 가지 Pythonic
표면을 제공한다.

```python
# 방안 A — 도구 정의에 데코레이터 한 번 (집행 가능, 권장)
from rein import Harness
h = Harness(record="run.jsonl")

@h.register_tool  # 도구 "정의"에만 붙는다. 호출부는 그대로.
def delete_file(path: str):
    os.remove(path)

# 방안 B — 컨텍스트 매니저로 하네스 수명주기 감싸기
# 도구는 위에서 이미 register_tool로 등록되어 있어야 한다 — with 블록이
# agent.run() 내부 호출을 자동으로 가로채지는 않는다. with는 스테이지
# 확정(seal)과 이벤트 저장소 close만 관리하는 수명주기 문법이다.
with h:
    agent.run(task="안 쓰는 파일 정리해줘")
```
```

(정확한 old_string/new_string은 Edit 도구로 파일을 다시 읽어 공백까지
그대로 맞춰 적용할 것 — 위 블록은 내용 참고용이며 문자 그대로 붙여넣지
않는다.)

- [ ] **Step 2: `src/rein/__init__.py` docstring 교체**

기존 파일 전체:

```python
"""rein: Agent = Model + Harness.

공개 API 표면은 CLAUDE.md §4에서 M1 시점에 확정되며 이후 시그니처를
바꾸지 않는다. 이 파일은 그 표면의 최상위 진입점이다.

    from rein import Harness

    h = Harness(record="run.jsonl")

    @h.register_tool
    def delete_file(path: str):
        ...

    with Harness(record="run.jsonl") as h:
        agent.run(task="...")
"""

from rein.guardrails.exceptions import (
    ApprovalRequired,
    Denied,
    GuardrailVerdictError,
    RetryRequested,
)
from rein.harness import Harness

__all__ = [
    "Harness",
    "GuardrailVerdictError",
    "Denied",
    "RetryRequested",
    "ApprovalRequired",
]
__version__ = "0.1.0"
```

교체 후(docstring만 변경, 나머지 동일):

```python
"""rein: Agent = Model + Harness.

공개 API 표면은 CLAUDE.md §4에서 M1 시점에 확정되며 이후 시그니처를
바꾸지 않는다. 이 파일은 그 표면의 최상위 진입점이다.

    from rein import Harness

    h = Harness(record="run.jsonl")

    @h.register_tool
    def delete_file(path: str):
        ...

    with h:
        agent.run(task="...")  # 도구는 위에서 이미 register_tool로 등록됨
"""

from rein.guardrails.exceptions import (
    ApprovalRequired,
    Denied,
    GuardrailVerdictError,
    RetryRequested,
)
from rein.harness import Harness

__all__ = [
    "Harness",
    "GuardrailVerdictError",
    "Denied",
    "RetryRequested",
    "ApprovalRequired",
]
__version__ = "0.1.0"
```

- [ ] **Step 3: 변경 확인**

Run: `grep -n "기존 코드 무수정" CLAUDE.md src/rein/__init__.py`
Expected: 출력 없음(문구가 완전히 제거됨)

Run: `pytest tests/ -v`
Expected: 전체 PASS(문서만 변경했으므로 회귀 없음 확인 차원)

- [ ] **Step 4: 커밋**

```bash
git add CLAUDE.md src/rein/__init__.py
git commit -m "docs: 방안 B(컨텍스트 매니저) 설명을 실제 동작과 일치하도록 정정"
```

---

## Task 4: LICENSE 추가

**Files:**
- Create: `LICENSE`

**Interfaces:** 없음(정적 파일).

- [ ] **Step 1: `LICENSE` 파일 생성**

`pyproject.toml`의 `license = {text = "MIT"}`, `authors = [{name = "rein
team"}]`과 일치하는 표준 MIT 라이선스 전문:

```
MIT License

Copyright (c) 2026 rein team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 2: 확인**

Run: `head -3 LICENSE`
Expected:
```
MIT License

Copyright (c) 2026 rein team
```

- [ ] **Step 3: 커밋**

```bash
git add LICENSE
git commit -m "chore: MIT LICENSE 추가"
```

---

## Task 5: `examples/quickstart_register_tool.py` (방안 A)

**Files:**
- Create: `examples/quickstart_register_tool.py`

**Interfaces:**
- Consumes: `rein.Harness`, `Harness.register_tool`(Task 1/2 변경과 무관,
  기존 공개 API 그대로).

- [ ] **Step 1: 디렉터리 확인**

Run: `ls examples 2>&1 || echo "not found"`
Expected: `not found`(신규 디렉터리이므로)

- [ ] **Step 2: 파일 생성**

`examples/quickstart_register_tool.py` 전체 내용:

```python
"""방안 A — @h.register_tool 데코레이터로 도구를 계측하는 최소 예제.

실행:
    python examples/quickstart_register_tool.py

examples/run_register_tool.jsonl에 tool_wrap + outcome 두 줄이 남는
것을 확인할 수 있다(*.jsonl은 .gitignore 대상이라 커밋되지 않는다).
"""

from pathlib import Path

from rein import Harness

RECORD_PATH = Path(__file__).parent / "run_register_tool.jsonl"

h = Harness(record=RECORD_PATH)


@h.register_tool
def add(a: int, b: int) -> int:
    return a + b


if __name__ == "__main__":
    print(add(2, 3))
    print(f"이벤트 로그: {RECORD_PATH}")
```

- [ ] **Step 3: 실행 확인**

Run: `python examples/quickstart_register_tool.py`
Expected:
```
5
이벤트 로그: .../examples/run_register_tool.jsonl
```

Run: `cat examples/run_register_tool.jsonl | wc -l`
Expected: `2`

- [ ] **Step 4: ruff 확인**

Run: `ruff check examples/quickstart_register_tool.py`
Expected: `All checks passed!`

- [ ] **Step 5: 커밋**

```bash
rm -f examples/run_register_tool.jsonl
git add examples/quickstart_register_tool.py
git commit -m "docs: 방안 A 퀵스타트 예제 추가"
```

---

## Task 6: `examples/quickstart_context_manager.py` (방안 B, 정정된 버전)

**Files:**
- Create: `examples/quickstart_context_manager.py`

**Interfaces:**
- Consumes: `rein.Harness`, `Harness.register_tool`, `Harness.__enter__`/`__exit__`.

- [ ] **Step 1: 파일 생성**

`examples/quickstart_context_manager.py` 전체 내용:

```python
"""방안 B — 컨텍스트 매니저로 하네스 수명주기를 감싸는 예제.

도구는 register_tool로 미리 등록되어 있어야 한다 — with 블록은
자동으로 도구 호출을 가로채지 않는다(CLAUDE.md §4 정정 참고). with는
스테이지 확정(seal)과 이벤트 저장소 close만 관리한다.

실행:
    python examples/quickstart_context_manager.py
"""

from pathlib import Path

from rein import Harness

RECORD_PATH = Path(__file__).parent / "run_context_manager.jsonl"

h = Harness(record=RECORD_PATH)


@h.register_tool
def delete_file(path: str) -> str:
    return f"deleted: {path}"


def agent_loop() -> None:
    print(delete_file(path="/tmp/scratch.txt"))


if __name__ == "__main__":
    with h:
        agent_loop()
    print(f"이벤트 로그: {RECORD_PATH}")
```

- [ ] **Step 2: 실행 확인**

Run: `python examples/quickstart_context_manager.py`
Expected:
```
deleted: /tmp/scratch.txt
이벤트 로그: .../examples/run_context_manager.jsonl
```

- [ ] **Step 3: ruff 확인**

Run: `ruff check examples/quickstart_context_manager.py`
Expected: `All checks passed!`

- [ ] **Step 4: 커밋**

```bash
rm -f examples/run_context_manager.jsonl
git add examples/quickstart_context_manager.py
git commit -m "docs: 방안 B 퀵스타트 예제 추가(register_tool + with 수명주기)"
```

---

## Task 7: `examples/rules.yaml` + `examples/guardrail_rule_yaml.py`

**Files:**
- Create: `examples/rules.yaml`
- Create: `examples/guardrail_rule_yaml.py`

**Interfaces:**
- Consumes: Task 1에서 고친 `Harness(rules=...)` 라이브 집행 배선.
  이 예제는 그 픽스가 없으면 `Denied`가 발생하지 않아 실패한다.

- [ ] **Step 1: `examples/rules.yaml` 생성**

전체 내용(CLAUDE.md §8 `rule_0007` 스타일):

```yaml
rule:
  id: rule_0007
  origin: auto
  when:
    tool: execute_sql
    features:
      class: { in: [DDL_DESTRUCTIVE] }
  scope:
    agent.role: content_editor
  then: deny
  rationale: "OWASP LLM06 Excessive Agency — content_editor는 파괴적 DDL 권한 없음"
```

- [ ] **Step 2: `examples/guardrail_rule_yaml.py` 생성**

전체 내용:

```python
"""rules.yaml로 라이브 실행 중 파괴적 SQL을 막는 예제.

Harness(rules=...)의 라이브 집행 배선(harness.py 버그 픽스 A) 덕분에,
등록한 규칙이 register_tool로 계측된 도구 호출에 실제로 적용된다.

실행:
    python examples/guardrail_rule_yaml.py
"""

from pathlib import Path

from rein import Denied, Harness

RULES_PATH = Path(__file__).parent / "rules.yaml"
RECORD_PATH = Path(__file__).parent / "run_guardrail.jsonl"

h = Harness(
    record=RECORD_PATH,
    rules=str(RULES_PATH),
    context={"agent_role": "content_editor"},
)


@h.register_tool
def execute_sql(query: str) -> str:
    return f"executed: {query}"


if __name__ == "__main__":
    print(execute_sql(query="SELECT * FROM notices"))

    try:
        execute_sql(query="DROP TABLE users;")
    except Denied as exc:
        print(f"차단됨: {exc}")
```

- [ ] **Step 3: 실행 확인**

Run: `python examples/guardrail_rule_yaml.py`
Expected:
```
executed: SELECT * FROM notices
차단됨: [DENY] rule_0007: OWASP LLM06 Excessive Agency — content_editor는 파괴적 DDL 권한 없음 (evt=evt_0002)
```

- [ ] **Step 4: ruff 확인**

Run: `ruff check examples/guardrail_rule_yaml.py`
Expected: `All checks passed!`

- [ ] **Step 5: 커밋**

```bash
rm -f examples/run_guardrail.jsonl
git add examples/rules.yaml examples/guardrail_rule_yaml.py
git commit -m "docs: rules.yaml 라이브 가드레일 예제 추가"
```

---

## Task 8: `examples/replay_verify_workflow.py` + `examples/README.md`

**Files:**
- Create: `examples/replay_verify_workflow.py`
- Create: `examples/README.md`

**Interfaces:**
- Consumes: 없음(신규 도구, rules 없이 실행해 replay CLI로 A/B를 보여주는
  용도 — Task 1 픽스와 무관하게 guardrail off 상태로 기록).

- [ ] **Step 1: `examples/replay_verify_workflow.py` 생성**

전체 내용:

```python
"""rein replay --compare / rein rule-from / rein report로 이어지는
CLI 워크플로용 run.jsonl을 생성하는 예제.

이 스크립트는 의도적으로 Harness(rules=...) 없이 실행한다 — 가드레일
off 상태로 기록해야 rein replay --compare가 off/on 차이를 보여줄 수
있다. replay-verify 자체는 실제 도구 호출이 필요 없어(CLAUDE.md §6)
CLI(`rein replay`)가 로그+rules만으로 단독 수행한다.

실행:
    python examples/replay_verify_workflow.py
    rein replay examples/run_workflow.jsonl --rules examples/rules.yaml --compare
    rein rule-from examples/run_workflow.jsonl --event evt_0003 -o examples/generated_rules.yaml --dry-run
    rein report examples/run_workflow.jsonl --rules examples/rules.yaml -o examples/report.html
"""

from pathlib import Path

from rein import Harness

RECORD_PATH = Path(__file__).parent / "run_workflow.jsonl"

h = Harness(record=RECORD_PATH, context={"agent_role": "content_editor"})


@h.register_tool
def execute_sql(query: str) -> str:
    return f"executed: {query}"


if __name__ == "__main__":
    execute_sql(query="SELECT * FROM notices")
    execute_sql(query="UPDATE notices SET title = 'ok' WHERE id = 1")
    execute_sql(query="DROP TABLE users;")  # 가드레일 off라 그대로 실행됨

    print(f"이벤트 로그: {RECORD_PATH}")
    print("다음 명령으로 이어서 확인:")
    print(f"  rein replay {RECORD_PATH} --rules examples/rules.yaml --compare")
```

- [ ] **Step 2: 실행 확인**

Run: `python examples/replay_verify_workflow.py`
Expected:
```
이벤트 로그: .../examples/run_workflow.jsonl
다음 명령으로 이어서 확인:
  rein replay .../examples/run_workflow.jsonl --rules examples/rules.yaml --compare
```

Run: `cat examples/run_workflow.jsonl | python3 -c "import sys,json; [print(json.loads(l)['evt'], json.loads(l)['source']) for l in sys.stdin]"`
Expected(3번째 tool_wrap이 DROP TABLE 호출이므로 `evt_0003`):
```
evt_0001 tool_wrap
evt_0001 outcome
evt_0002 tool_wrap
evt_0002 outcome
evt_0003 tool_wrap
evt_0003 outcome
```

- [ ] **Step 3: CLI 이어서 확인**

Run: `rein replay examples/run_workflow.jsonl --rules examples/rules.yaml --compare`
Expected: `evt_0003`(DROP TABLE) 행에서 off=`allow`, on=`deny`로 갈리는
비교 표가 출력됨(off는 기록된 그대로 allow — 이 스크립트가 rules 없이
실행했으므로 정상)

- [ ] **Step 4: `examples/README.md` 생성**

전체 내용:

```markdown
# rein examples

각 스크립트는 리포지토리 루트에서 그대로 실행 가능하다
(`pip install -e .` 이후). `demo/ab_demo/`는 별도의 대회 발표용 A/B
데모 자산이며, 여기 예제와는 독립적으로 유지된다.

| 스크립트 | 보여주는 것 |
|---|---|
| `quickstart_register_tool.py` | 방안 A — `@h.register_tool` 데코레이터 최소 통합 |
| `quickstart_context_manager.py` | 방안 B — `with h:`로 하네스 수명주기 감싸기(도구는 여전히 `register_tool`로 먼저 등록) |
| `guardrail_rule_yaml.py` (+ `rules.yaml`) | `Harness(rules=...)`가 라이브 실행 중 파괴적 SQL을 실제로 막는 예제 |
| `replay_verify_workflow.py` | `run.jsonl`을 생성해 `rein replay --compare` / `rein rule-from` / `rein report` CLI로 이어가는 워크플로 |

## 실행

```bash
python examples/quickstart_register_tool.py
python examples/quickstart_context_manager.py
python examples/guardrail_rule_yaml.py
python examples/replay_verify_workflow.py
rein replay examples/run_workflow.jsonl --rules examples/rules.yaml --compare
```

실행 중 생성되는 `*.jsonl`/`*.html` 산출물은 `.gitignore`로 이미
제외되어 커밋되지 않는다.
```

- [ ] **Step 5: ruff 확인**

Run: `ruff check examples/replay_verify_workflow.py`
Expected: `All checks passed!`

- [ ] **Step 6: 커밋**

```bash
rm -f examples/run_workflow.jsonl examples/report.html examples/generated_rules.yaml
git add examples/replay_verify_workflow.py examples/README.md
git commit -m "docs: replay-verify CLI 워크플로 예제 + examples 인덱스 추가"
```

---

## Task 9: README.md 전면 재작성 (영어 + 한국어 병기)

**Files:**
- Modify: `README.md` (전체 교체)

**Interfaces:** 없음(문서 전용). Task 5~8에서 만든 `examples/` 파일
경로를 그대로 링크한다.

- [ ] **Step 1: `README.md` 전체 교체**

전체 내용:

```markdown
# rein (고삐)

`Agent = Model + Harness`

Framework-agnostic safety/observability middleware for AI agents. Moves
correctness and safety responsibility out of the (probabilistic) prompt
and into deterministic infrastructure — "harness engineering."

[English](#english) | [한국어](#한국어)

---

## English

### What makes rein different

Real-time blocking and replay already exist elsewhere (MS Agent
Governance Toolkit, LangGraph HITL time-travel checkpoints). rein's one
genuinely new piece is the **failure → rule loop**: find a failure in a
replayed log, synthesize a deny rule from it on the spot, and confirm the
same log is now blocked — with the candidate regression-tested against a
corpus of known-good calls before it's ever adopted. Interception and
JSONL recording are supporting features, not the pitch.

### Install

```bash
pip install rein
```

### Quickstart

**Option A — decorate the tool definition (recommended, enforceable):**

```python
from rein import Harness

h = Harness(record="run.jsonl")

@h.register_tool  # attaches to the tool's definition, not each call site
def delete_file(path: str):
    ...
```

**Option B — wrap the harness lifecycle with a context manager:**

Tools must still be registered with `register_tool` first — the `with`
block does not auto-intercept calls inside `agent.run()`. It only manages
the harness's lifecycle (sealing the stage pipeline, closing the event
store).

```python
h = Harness(record="run.jsonl")

@h.register_tool
def delete_file(path: str):
    ...

with h:
    agent.run(task="clean up unused files")
```

### CLI

| Command | What it does |
|---|---|
| `rein seed <run.jsonl>` | Validate a recorded log (schema + zero critical outcomes) and promote it to `golden_run.jsonl` |
| `rein replay <run.jsonl> [--rules rules.yaml ...] [--compare]` | Replay-verify: reapply `rules.yaml` against a recorded log; `--compare` shows guardrail off vs on side by side |
| `rein rule-from <run.jsonl> --event evt_XXXX [--golden golden_run.jsonl] [--dry-run]` | Synthesize a deny rule from a failure event, regression-tested against the golden/synthetic-negative corpus |
| `rein report <run.jsonl> --rules rules.yaml [-o report.html]` | Render a static HTML report: branch timeline, before/after metrics, candidate regression table, adopted-rule regression matrix |

### Guardrail rules example

```python
from rein import Denied, Harness

h = Harness(
    record="run.jsonl",
    rules="rules.yaml",
    context={"agent_role": "content_editor"},
)

@h.register_tool
def execute_sql(query: str) -> str:
    return run(query)

execute_sql(query="SELECT * FROM notices")  # passes
try:
    execute_sql(query="DROP TABLE users;")  # blocked by rules.yaml
except Denied as exc:
    print(exc)
```

See [`examples/`](examples/) for runnable versions of both quickstart
options, the guardrail example above, and a full `rein replay
--compare` / `rein rule-from` / `rein report` workflow. The competition
A/B demo assets live separately in [`demo/ab_demo/`](demo/ab_demo/).

### Architecture at a glance

```
[agent loop] -> (interceptor: single choke point) -> [tool / environment]
       |                  |
       |            check -> guardrail pipeline (allow/deny/retry/approve)
       |            record -> event store (append-only JSONL)
       |                  |
       |            [replay engine] deterministic replay
       |                  |
       [timeline UI] -> (pick a failure) -> [rule generator] -> re-verify
```

Every tool call passes through one interceptor. Model-client observation
(`observe_model`) is opt-in and record-only — it never enforces a
verdict, so it never implies a false sense of "this will get blocked."

### License

MIT — see [LICENSE](LICENSE).

---

## 한국어

### 차별점

실시간 차단과 리플레이는 이미 다른 곳(MS Agent Governance Toolkit,
LangGraph HITL time-travel 체크포인트)에 있다. rein이 진짜로 새로
내놓는 한 가지는 **실패 → 규칙 생성 루프**다 — 리플레이한 로그에서
실패를 찾아 그 자리에서 deny 규칙을 합성하고, 같은 로그를 다시
리플레이해 실제로 막히는지 확인한다. 채택 전에는 반드시 기존 정상
호출 코퍼스로 회귀 검증을 통과해야 한다. 인터셉션과 JSONL 기록은
이 루프를 받쳐주는 기능이지 그 자체가 서사가 아니다.

### 설치

```bash
pip install rein
```

### 퀵스타트

**방안 A — 도구 정의에 데코레이터 한 번(권장, 집행 가능):**

```python
from rein import Harness

h = Harness(record="run.jsonl")

@h.register_tool  # 도구 "정의"에만 붙는다. 호출부는 그대로.
def delete_file(path: str):
    ...
```

**방안 B — 컨텍스트 매니저로 하네스 수명주기 감싸기:**

도구는 여전히 `register_tool`로 먼저 등록되어 있어야 한다 — `with`
블록이 `agent.run()` 내부 호출을 자동으로 가로채지는 않는다. 스테이지
확정(seal)과 이벤트 저장소 close만 관리하는 수명주기 문법이다.

```python
h = Harness(record="run.jsonl")

@h.register_tool
def delete_file(path: str):
    ...

with h:
    agent.run(task="안 쓰는 파일 정리해줘")
```

### CLI

| 명령 | 하는 일 |
|---|---|
| `rein seed <run.jsonl>` | 기록된 로그를 검증(스키마 + critical outcome 0건)하고 `golden_run.jsonl`로 지정 |
| `rein replay <run.jsonl> [--rules rules.yaml ...] [--compare]` | replay-verify: 기록된 로그에 `rules.yaml`을 재적용. `--compare`는 가드레일 off/on을 나란히 비교 |
| `rein rule-from <run.jsonl> --event evt_XXXX [--golden golden_run.jsonl] [--dry-run]` | 실패 이벤트에서 deny 규칙을 합성하고 golden/합성 음성 코퍼스로 회귀 검증 |
| `rein report <run.jsonl> --rules rules.yaml [-o report.html]` | 정적 HTML 리포트 렌더링: 분기 타임라인, before/after 지표, 후보 회귀 표, 채택 규칙 회귀 매트릭스 |

### 가드레일 규칙 예제

```python
from rein import Denied, Harness

h = Harness(
    record="run.jsonl",
    rules="rules.yaml",
    context={"agent_role": "content_editor"},
)

@h.register_tool
def execute_sql(query: str) -> str:
    return run(query)

execute_sql(query="SELECT * FROM notices")  # 통과
try:
    execute_sql(query="DROP TABLE users;")  # rules.yaml에 의해 차단
except Denied as exc:
    print(exc)
```

위 두 퀵스타트 방안과 가드레일 예제, `rein replay --compare` / `rein
rule-from` / `rein report` 전체 워크플로를 직접 실행해볼 수 있는 버전은
[`examples/`](examples/)에 있다. 대회 A/B 데모 자산은 별도로
[`demo/ab_demo/`](demo/ab_demo/)에 있다.

### 아키텍처 한눈에 보기

```
[에이전트 루프] → (인터셉터: 단일 길목) → [도구/환경]
       │                  │
       │            검사 → 가드레일 파이프라인 (allow/deny/retry/approve)
       │            기록 → 이벤트 저장소 (append-only JSONL)
       │                  │
       │            [리플레이 엔진] 결정론 재생
       │                  │
       [타임라인 UI] → (실패 선택) → [규칙 생성기] → 재검증
```

모든 도구 호출은 하나의 인터셉터를 통과한다. 모델 클라이언트 관측
(`observe_model`)은 기본 비활성/옵트인이며 기록 전용이다 — 판정을
내리지 않으므로 "막아줄 것"이라는 거짓 안전감을 주지 않는다.

### 라이선스

MIT — [LICENSE](LICENSE) 참고.

---

## Development / 기여

```bash
git clone https://github.com/SeoYeongBaek/rein.git
cd rein
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install
pytest
```

```
src/rein/
  harness.py       # 공개 API (Harness)
  cli.py           # rein seed / replay / rule-from / report
  guardrails/      # 가드레일 파이프라인
  events/          # 이벤트 저장소 (JSONL)
  adapters/        # 모델 어댑터 (OpenAI / Anthropic / local)
  replay/          # 리플레이 엔진
  rules/           # 규칙 생성 엔진
tests/
examples/          # OSS 온보딩용 최소 예제
demo/ab_demo/      # 대회 A/B 데모 자산
```

전체 설계는 [`CLAUDE.md`](CLAUDE.md)를 참고.
```

- [ ] **Step 2: 링크 대상 존재 확인**

Run: `ls LICENSE examples/README.md demo/ab_demo/report.html CLAUDE.md`
Expected: 네 경로 모두 존재(파일 없음 에러 없이 나열됨)

- [ ] **Step 3: 커밋**

```bash
git add README.md
git commit -m "docs: README를 영어+한국어 병기로 전면 재작성"
```

---

## 최종 확인

- [ ] **Step 1: 전체 테스트 스위트**

Run: `pytest tests/ -v`
Expected: 전체 PASS(`test_intentional_fail.py`의 xfail 1건 제외)

- [ ] **Step 2: ruff 전체**

Run: `ruff check .`
Expected: `All checks passed!`

- [ ] **Step 3: pre-commit 전체**

Run: `pre-commit run --all-files`
Expected: 모든 훅 통과
