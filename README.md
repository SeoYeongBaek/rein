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
