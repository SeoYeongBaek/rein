# M3 설계: OSS 공개 준비 (README · LICENSE · 예제 · rules 라이브 집행 픽스)

날짜: 2026-07-21
작성: 서영 (브레인스토밍 세션)
관련 마일스톤: CLAUDE.md §12 M3
브랜치: `sy/spec/M3`

## 배경

M1·M2는 완료되어 인터셉터·가드레일 파이프라인·이벤트 저장소·리플레이
엔진·규칙 생성 엔진·`report.html` 렌더링까지 전부 구현되어 있다(이슈
#7~#65 전부 closed). M3(§12)는 "README + 어댑터 2종 + 라이선스 + 예제
(API는 M1에서 고정, 신규 설계 없음)"이다.

어댑터 2종(OpenAI/Anthropic)은 이미 M1 단계에서 구현되어 있어 M3에서
새로 만들 필요가 없다. 따라서 이번 M3의 실질 범위는 README 작성,
LICENSE 추가, 예제 정비다.

다만 이 작업을 준비하는 과정에서 코드를 검증하다가, "신규 설계 없음"
원칙과는 별개로 **기존 구현이 이미 확정된 계약을 어기고 있는 버그
두 건**을 발견했다. OSS 첫인상이 되는 README/예제가 실제로 동작하지
않는 코드를 보여주게 되는 상황이라, 이번 M3 범위에 이 두 버그 픽스를
포함하기로 결정했다(사용자 확인 완료).

## 발견한 버그 두 건

### 버그 1 — `Harness(rules=...)`가 라이브 실행에서 집행되지 않음

`Harness.__init__`(harness.py:129)이 `rules` 인자를 `self.rules`에
저장만 하고, 실제 가드레일 파이프라인의 `_default_safety_check`
(harness.py:381)은 이 값을 전혀 참조하지 않는 스텁(`Verdict.ALLOW`
고정 반환)이다. `rules.yaml` 로딩·판정(`_load_rules`, `rule_matches`,
`_verdict_from_rules`)은 현재 `cli.py`의 `rein replay --rules` 경로에만
연결되어 있다.

결과: `Harness(record=..., rules="rules.yaml")`로 `register_tool`된
도구를 실행해도, 그 도구 호출이 rules.yaml에 매칭되는 deny 규칙을
가지고 있어도 실제로 막히지 않는다. CLAUDE.md §4/§5가 약속한 "채택된
규칙은 런타임에 결정론적으로 집행된다"는 계약과 어긋난다.

### 버그 2 — deny/retry/approve 판정이 이벤트 저장소에 전혀 기록되지 않음

`_intercept`(harness.py:251)는 가드레일 파이프라인에서 첫 non-allow
판정이 나오면 `_enforce()`로 즉시 예외를 던지고 리턴한다. `record_tool_wrap`
호출은 이 지점을 통과한 뒤(즉 allow가 확정된 뒤)에만 등장한다. 결과:
막힌 호출은 JSONL에 아예 한 줄도 남지 않는다.

이는 §9가 이미 설계해둔 "tool_wrap 줄과 outcome 줄의 생애주기가 다를
수 있다(실행이 예외로 끊기면 outcome 자체가 없을 수 있다)"는 전제와
모순된다 — outcome이 없을 수 있다는 것이지, tool_wrap 자체가 없어도
된다는 뜻이 아니다. 인터셉터의 정의(§3) 자체가 "① 검사 → 가드레일
파이프라인 ② 기록 → 이벤트 저장소"인데, 지금은 막힌 호출에 대해
①만 있고 ②가 빠져 있다.

## 코드 수정 범위

### A. `_default_safety_check`에 rules.yaml 실제 연결

- `Harness.__init__`에서 `rules`(`str | list[str] | None`)를 경로
  리스트로 정규화한 뒤, 생성 시점에 `rein.rules.runtime._load_rules`로
  즉시 로드해 `self._loaded_rules`에 캐싱한다. YAML이 없거나 파싱
  실패하면 `Harness()` 생성 시점에 바로 에러(§5 fail-closed와 동일한
  타이밍 원칙 — 첫 도구 호출까지 에러를 미루지 않는다).
- `_default_safety_check(tool_call, ctx)`는 `tool_call`을
  `{"tool_name": tool_call["name"], "args": tool_call.get("args", {}),
  "context": ctx or {}}` 형태의 evt로 변환해 `rein.rules.runtime`의
  `matching_rules`/`_verdict_from_rules`/`_to_verdict`를 그대로
  재사용한다. 새 판정 로직·새 우선순위 매핑을 만들지 않는다(§5 결정
  — "별도 VERDICT_PRIORITY 매핑 dict는 두지 않는다"와 같은 원칙을
  라이브 경로에도 적용).
- 매칭된 규칙이 없거나 최종 verdict가 allow면 `(Verdict.ALLOW, "",
  "", "")`. non-allow면 승리한 규칙의 `id`/`rationale`을
  `rule_id`/`rationale`로 채운다.
- `self.rules`가 `None`이면 기존과 동일하게 항상 allow(하위 호환 —
  rules를 안 주는 기존 사용자 코드는 그대로 동작).

### B. deny/retry/approve도 이벤트 저장소에 기록

- `_intercept`를 재구성해, 파이프라인에서 non-allow가 나온 즉시
  `record_tool_wrap(..., verdict=str(verdict))`을 먼저 호출해 실제
  판정값으로 한 줄을 남긴 뒤 예외를 던진다. outcome 줄은 만들지
  않는다(실행 자체가 없었으므로 §9 생애주기상 자연스럽게 없음).
- 이 경로에서 예외에 실어 보내는 `evt_id`는 스테이지 함수가 반환한
  4번째 값(placeholder) 대신, 방금 기록된 이벤트의 실제 `evt` 필드를
  쓴다. 스테이지 저자는 실제 evt id를 미리 알 수 없으므로(부여는
  `EventStore`의 책임), 하네스가 기록 시점에 진짜 id로 덮어쓰는 편이
  "`rein rule-from --event evt_XXXX`로 정확히 이 호출을 가리킨다"는
  §9 약속에 더 부합한다.
- allow 경로는 기존과 동일(실행 후 `record_ok`/`record_error`).

### C. 테스트

TDD로 진행한다(픽스 전에 실패하는 테스트를 먼저 작성).

- 신규: `Harness(record=..., rules="rules.yaml", context={"agent_role":
  "content_editor"})`로 등록한 `execute_sql` 도구에 안전한 쿼리(allow)와
  `DROP TABLE`(deny) 각각을 호출해, deny 시 `Denied`가 실제로 발생하는지
  검증.
- 신규: 위 deny 호출 후 JSONL에 `source=="tool_wrap"`, `verdict=="deny"`
  줄이 정확히 기록되고, 대응하는 `outcome` 줄은 없음을 검증. 발생한
  `Denied.evt_id`가 그 줄의 `evt` 필드와 일치하는지도 검증.
- 기존 회귀: `tests/test_harness_issue_29.py`(allow 경로 verdict=="allow"
  기록), `tests/test_harness_issue_33.py`, `tests/test_intentional_fail.py`
  (커스텀 스테이지 deny 경로)는 evt_id 정확값을 assert하지 않으므로
  그대로 통과해야 한다 — 통과 여부를 실행해 재확인한다.

## 문서 수정 범위

### CLAUDE.md §4 "방안 B" 재서술

현재 문구("기존 코드 무수정")는 컨텍스트 매니저가 `agent.run()` 내부
도구 호출을 자동으로 가로챈다는 오해를 준다. 실제로는 도구가 여전히
`@h.register_tool`로 등록돼 있어야 하고, `with Harness(...) as h`는
그 하네스 인스턴스의 수명주기(스테이지 seal, 이벤트 저장소 close)만
관리하는 문법이다. §4 예시를 다음 형태로 정정한다:

```python
h = Harness(record="run.jsonl")

@h.register_tool
def delete_file(path: str):
    ...

with h:
    agent.run(task="안 쓰는 파일 정리해줘")  # 도구 정의는 위에서 이미 등록됨
```

같은 정정을 `src/rein/__init__.py` 최상단 docstring 예시에도 적용한다.

## LICENSE

MIT. `Copyright (c) 2026 rein team` — `pyproject.toml`의
`license = {text = "MIT"}`, `authors = [{name = "rein team"}]`과 일치.

## README.md

단일 파일, 영어 섹션(상단) + 한국어 섹션(하단) 구성. 상단에 두 섹션을
가리키는 앵커 내비게이션(`[English](#english) | [한국어](#한국어)`)을
둔다. 두 섹션 모두 동일한 목차를 갖는다:

1. 한 줄 정의(`Agent = Model + Harness`) + 포지셔닝 문구(§1)
2. 차별점 — 기둥 3(실패→규칙→재검증 루프)에 집중, 기둥 1·2는 기능으로만
   언급(§2 규칙 그대로 반영 — 서사에서 경쟁하지 않음)
3. 설치 (`pip install rein`)
4. 퀵스타트 — 방안 A(`register_tool`)와 방안 B(컨텍스트 매니저, 정정된
   버전) 코드 블록. 방안 B는 위 CLAUDE.md 수정과 동일하게 정확히 서술.
5. CLI 개요 — `rein seed` / `rein replay` / `rein rule-from` / `rein
   report` 한 줄 요약 표
6. 가드레일 규칙 예제 — `examples/guardrail_rule_yaml.py` 스니펫 요약
7. 더 보기 — `examples/` 링크, `demo/ab_demo/`(대회 A/B 데모) 링크
8. 아키텍처 한눈에 보기 — §3 다이어그램 축약
9. 라이선스 — MIT, LICENSE 링크

## `examples/` (신규 디렉터리)

기존 `demo/ab_demo/`(대회 발표용 A/B 데모 자산)와는 별도로, OSS
온보딩용 최소 예제를 새로 둔다. `demo/ab_demo/`는 손대지 않는다.

- `examples/README.md` — 각 스크립트 설명과 실행 방법 인덱스.
- `examples/quickstart_register_tool.py` — 방안 A. 순수 Python 루프에서
  `@h.register_tool` 데코레이터 하나로 도구를 계측하고, JSONL에 두 줄
  (tool_wrap/outcome)이 남는 것을 보여준다.
- `examples/quickstart_context_manager.py` — 방안 B(정정된 버전).
  도구는 `register_tool`로 미리 등록하고, `with h:` 블록으로 실행
  구간의 수명주기를 감싸는 정확한 패턴을 보여준다.
- `examples/rules.yaml` — CLAUDE.md §8 `rule_0007` 스타일: `execute_sql`
  도구에서 `content_editor` role이 `DDL_DESTRUCTIVE` class를 호출하면
  deny.
- `examples/guardrail_rule_yaml.py` — 버그 픽스 A 덕분에 실제로 동작하는
  예제. `Harness(rules="rules.yaml", context={"agent_role":
  "content_editor"})`로 등록한 `execute_sql`에 안전한 쿼리(allow 통과)와
  `DROP TABLE`(deny, `Denied` 예외 발생)을 각각 호출해 대비를 보여준다.
- `examples/replay_verify_workflow.py` — `register_tool`로 도구를
  계측해 `run.jsonl`을 생성하는 스크립트 + 스크립트 하단 주석/print로
  이어서 실행할 CLI 워크플로(`rein seed golden_run.jsonl` → 이 스크립트
  실행 → `rein replay run.jsonl --rules rules.yaml --compare` → `rein
  rule-from run.jsonl --event evt_XXXX` → `rein report`) 안내. §4 CLI
  명세 그대로, 새 CLI 옵션은 만들지 않는다.

## 스코프 밖

- `rein rule-from`/`rein report`/리플레이 엔진 등 기존 CLI·엔진 로직
  자체의 신규 기능 추가는 하지 않는다(§12 M3 "신규 설계 없음").
- 로컬 어댑터(`LocalAdapter`)의 실제 응답 포맷 구현은 M4 스코프 그대로
  둔다(§3 TODO 현준 확정 항목, 이번 PR에서 건드리지 않음).
- deny 판정에 대한 예산(budget)·권한(permission)·스키마(schema) 스테이지
  자체의 실제 검증 로직 구현은 이번 스코프가 아니다(safety 스테이지의
  rules.yaml 와이어링만 다룬다). 세 스텁은 그대로 둔다.
- `_intercept`의 deny 기록 픽스는 tool_wrap 줄만 다루며, 새로운 outcome
  상태값(예: `"status": "denied"`)을 §9 스키마에 추가하지 않는다 —
  outcome 줄 자체가 생기지 않는 것으로 충분하다.

## 브랜치·이슈 전략

이 스펙은 `sy/spec/M3` 브랜치에 커밋한다. 구현 단계(writing-plans 이후)는
아래처럼 작업 단위별로 별도 이슈·브랜치로 나눠 진행한다(레포 기존 관례—
`{이름}/{종류}/{이슈번호 또는 슬러그}-{설명}`):

- harness rules 라이브 집행 픽스 (버그 1)
- harness deny 이벤트 기록 픽스 (버그 2)
- README + LICENSE
- examples/ 디렉터리
- CLAUDE.md §4 방안 B 문서 정정 (코드 픽스와 같은 PR에 포함 가능)
