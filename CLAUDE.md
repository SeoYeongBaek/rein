# CLAUDE.md

이 문서는 rein 프로젝트에서 작업할 때 Claude가 따라야 할 지침이다.
코드베이스는 아직 없으므로
이 문서의 모든 기술적 사실은 기획서에서 직접 가져왔거나, 기획서의 원칙을
근거로 명시적으로 확정한 것이다. 근거 없이 추측해서 채운 내용은
없다.

## 1. 프로젝트 정체성

- 이름: **rein**. `pip install rein`으로 설치되는 **프레임워크
  비종속 미들웨어 라이브러리**다. 웹앱이 아니다.
- 한 줄 정의: `Agent = Model + Harness`. 안전성·실행 정확성 책임을
  프롬프트(확률적)에서 빼내 결정론적 인프라로 옮기는 것이 "하네스
  엔지니어링"이다.
- 한 줄 포지셔닝: "특정 프레임워크 밖 에이전트를 위한 안전벨트 — 사람
  없이 자동으로 막고, 실패를 규칙으로 굳혀 다시는 같은 실수를 못 하게
  한다."
- 2026 소프트웨어공모전 제출 프로젝트이면서, 동시에 향후 OSS 공개를
  전제로 설계한다. **공개 API 표면은 M1부터 OSS 기준으로 고정**하고
  이후 시그니처를 바꾸지 않는다 (§4 제약).

## 2. 차별점 — 기둥 3에만 집중

2026년 시장 기준으로 "실시간 차단"과 "리플레이"는 이미 MS Agent
Governance Toolkit, LangGraph HITL(time-travel 체크포인트) 등이
선점해 더 이상 신규성이 아니다.

| 기둥 | 내용 | 판정 |
|---|---|---|
| 1. 프레임워크/프로바이더 비종속 | LangGraph HITL은 LangGraph 안에서만 동작 | 약화(주장 안 함) |
| 2. 사람 없는 자동 규칙 | 미들웨어형 가드레일과 겹침 | 약화(주장 안 함) |
| 3. 실패 → 규칙 생성 루프 | 아직 빈자리 | **유일한 차별점 — 여기에 집중** |

**규칙**: 기둥 1·2는 기능으로는 유지하되 서사·데모·문서·코드 주석에서
그것으로 경쟁하려 하지 않는다. 모든 것은 기둥 3(리플레이에서 실패 발견
→ 그 자리에서 규칙 생성 → 같은 로그 재리플레이로 차단 확인)으로
수렴시킨다.

**절대 금기**: LangGraph의 클론을 만들지 않는다. 에이전트 루프는
미니멀하게 직접 짜서 "프레임워크가 아니라 미들웨어"라는 정체성을
지킨다.

## 3. 아키텍처 — 단일 길목(인터셉터)

```
[에이전트 루프] → (인터셉터: 단일 길목) → [도구/환경]
       │                  │
       │            ① 검사 → 가드레일 파이프라인 (allow/deny/retry/approve)
       │            ② 기록 → 이벤트 저장소 (append-only JSONL)
       │                  │
       │            [리플레이 엔진] 결정론 재생 (record / replay-verify / live-rerun)
       │                  │
       │            [타임라인 UI] → (실패 선택) → [규칙 생성기] → 재검증
```

- 모든 도구 호출이 반드시 통과하는 **단일 지점**이 인터셉터다. 에이전트가
  도구 호출을 제안하면 실행 전에 가로채 ① 가드레일에 넘기고 ② 이벤트로
  기록한다.
- 모델 어댑터(OpenAI·Claude·로컬)를 통해 프로바이더 비종속을 확보한다.
- **인터셉션 표면은 정직하게 둘로 한정**한다 (과장 금지), 그리고 **두
  표면은 완전히 분리된 별도 함수**로 구현한다 — 하나의 `_intercept`로
  억지로 합치지 않는다.

  | 표면 | 진입점 | 무엇을 잡나 | 집행 가능? | 판정 발생? | 용도 |
  |---|---|---|---|---|---|
  | 모델 클라이언트 래핑 | `_observe(model_response)` | LLM이 제안한 tool_use | ✗ (실행은 유저 코드) | 아니오, 기록만 | 관측 |
  | 도구 래핑 | `_intercept(tool_call)` | 실제 도구 실행 | ✓ | 예 (allow/deny/retry/approve) | 집행(권장) |

  집행 불가능한 표면(모델 클라이언트 래핑)에 가드레일 판정을 걸면
  "막아줄 것"이라는 거짓 안전감을 준다 — 그래서 이 표면은 기록 전용으로
  못박는다. 정확한 주장은 "도구 또는 모델 클라이언트를 감쌀 수 있는
  모든 에이전트에, 최소 리팩토링으로"다. "임의의 에이전트에 무조건
  붙는다" 식으로 과장하지 않는다.
- **모델 클라이언트 관측은 기본 비활성, 옵트인이다.**
  ```python
  h = Harness(record="run.jsonl")   # 기본: 도구 래핑(_intercept)만 켜짐
  h.observe_model(client)            # 명시적으로 켜야 _observe 관측 시작
  ```
  기본값을 도구 래핑 단독으로 둬서, 사용자가 의식적으로 선택하지 않는 한
  이중 기록(같은 행동이 관측+집행 양쪽에서 찍히는 것)이 애초에 발생하지
  않게 한다.

## 4. 공개 API — M1에 확정, 이후 불변

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

이 시그니처("5줄 통합")는 M1 시점에 확정하고, M2~M4에서 바꾸지 않는다.
CLI 표면(`rein seed`, `rein replay`, `rein rule-from`, `rein report`)도
동일하게 공개 인터페이스로 취급한다.

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

### 개발자 경험 흐름 (온보딩)

1. `pip install rein`
2. (권장, 강제 아님) `rein seed`로 정상 시나리오를 `golden_run.jsonl`에
   녹화해 음성 베이스라인 확보
3. 도구에 `@h.register_tool` 부착 후 평소대로 실행 → `run.jsonl` 누적
4. `rein replay run.jsonl --compare`로 가드레일 off/on A/B 비교,
   `rein rule-from ... --event evt_0042`로 `rules.yaml` 생성·재검증
5. `rein report run.jsonl -o report.html`로 결과를 정적 HTML로 시각화

**하드 게이트 없음**: golden 코퍼스가 없어도 온보딩(2·3단계)은 정상
동작한다. 골든 트레이스는 "강력 권장"이지 라이브러리 초기화의 필수
조건이 아니다.

## 5. 가드레일 파이프라인

순서가 있는 결정론적 체크. 각 단계는 `allow / deny / retry / approve`
중 하나를 반환한다. 이 4단계는 고정 법칙이 아니라 **기본 정책
번들**이며, 실제 설정 표면은 ① 순서 있는 체크 스테이지 목록과 ② scope
술어로 한정된 YAML 룰셋 두 가지다.

| 순서 | 체크 | 막는 것 |
|---|---|---|
| 1 | 스키마 검증 | 도구 호출 인자가 규격에 맞는지 |
| 2 | 권한 체크 | 이 에이전트가 이 도구를 부를 자격이 있는지 |
| 3 | 예산 체크 | 토큰·비용·실행 시간·무한 루프 |
| 4 | 안전 규칙 | 위험 명령 패턴, 기밀 데이터 유출 |

가장 값싼 체크(형식 오류, 자격 없음)를 먼저, 가장 비싼 분석(안전
규칙)을 마지막에 둔다. 평가는 **short-circuit(fail-fast)** 방식으로
schema → permission → budget → safety 순서를 지킨다.

**여러 스테이지가 서로 다른 판정을 낼 때의 충돌 해결 우선순위**:
`deny > approve > retry > allow`. 가장 제한적인 판정이 이긴다.

집행 엔진 자체는 **순서 있는 함수 리스트 + 첫 non-allow 승리**로
구현한다. OPA/Cedar 같은 외부 정책 엔진은 절대 쓰지 않는다 — 별도
런타임·새 DSL·비-Python 의존성이 "5줄 통합·미들웨어 하나" 정체성을
깨기 때문이다.

**"순수 함수"의 정확한 의미**: 스테이지 함수는 전역 변수나 클로저에
숨은 상태를 갖지 않는다(no hidden state). 단, `Context` 객체를 통해
세션 누적 상태(토큰 사용량, 호출 횟수 등)를 명시적으로 주입받아 읽고
갱신할 수 있다 — budget 체크처럼 호출 간 누적이 필요한 체크는 이
방식으로 구현한다. 상태가 없다는 뜻이 아니라, 상태가 함수 시그니처에
드러난 의존성으로만 존재한다는 뜻이다.

### 스테이지 확장 인터페이스

새 검사 로직은 `Callable[[ToolCall, Context], Verdict]` 프로토콜을
따르는 함수로 등록한다. 새 플러그인 레지스트리나 외부 DSL은 만들지
않는다.

```python
h.register_stage("safety_v2", my_custom_stage)  # 로직 정의는 Python
```

**순서 재배열은 YAML이 담당**한다(로직은 코드, 순서는 설정이라는
역할 분리):
```yaml
# rein.yaml
stage_order: [schema, permission, budget, safety_v2]
```

**fail-closed 원칙**: `stage_order`가 미등록 스테이지 이름을 참조하면
`Harness()` 생성 자체를 즉시 실패시킨다(초기화 시점, 런타임 아님).
오타나 미등록 이름을 조용히 무시하면 "안전 실패"가 아니라 "안전
미실행" 상태로 돌아갈 위험이 있으므로, 조용한 무시는 절대 금지한다.

## 6. 이벤트 저장소 + 리플레이

- 인터셉터가 뱉는 구조화 이벤트를 append-only JSONL로 쌓는다. 이벤트
  구성: `{시점, 제안된 행동, 가드레일 판정, 도구 결과, 컨텍스트 변화}`.
- **리플레이 결정론이 프로젝트의 기술적 핵심(1순위 설계)**이다. LLM은
  비결정론적이라 그냥 재실행하면 두 번째 실행에서 모델이 다른 행동을
  제안할 수 있어 A/B가 깨진다. 해결책: 리플레이 시 **LLM을 다시
  호출하지 않고**, 1차 실행에서 녹화한 행동 시퀀스를 그대로 인터셉터에
  다시 흘려보낸다(VCR 방식). 모델 응답도 도구 응답도 녹화-재생하며,
  바뀌는 변수는 오직 "가드레일 on/off" 하나여야 한다.
- **세 가지 replay 모드**: record / replay-verify / live-rerun.
- **vcrpy는 쓰지 않는다.** vcrpy는 소켓 레벨을 후킹하는데, 비-HTTP
  로컬 도구(파일 삭제 등)는 함수 레벨에서 별도로 녹화해야 해서 두
  녹화 영역의 타임스탬프·순번이 어긋나는 "상태 비대칭(State
  Asymmetry)" 문제가 생긴다. 대신 이미 확보한 단일 길목(인터셉터)에서
  LLM·모든 도구의 입출력을 **동일한 JSONL 스키마 + 단일 순번
  카운터**로 일원화 녹화하는 자체 미니멀 레코더(약 50줄)를 쓴다. VCR
  패턴(record-once/replay-many)은 따르되 라이브러리는 쓰지 않는다.
- **정직한 한계**: 깨끗한 정량 A/B는 첫 개입 지점까지만 성립한다(개입
  후에는 녹화 시퀀스가 무의미해짐). 이 한계는 숨기지 않고 먼저
  선언한다.

### 인자 매칭 규칙 (replay-verify)

**위치(시퀀스 인덱스) 기반 매칭이 원칙이다. 인자 값은 비교하지 않는다.**
n번째 인터셉트 호출은 무조건 로그의 n번째 `tool_wrap` 이벤트에
대응시킨다.

- `tool_name` 불일치는 즉시 하드 에러(로그-실행 순서 어긋남 감지).
- `args`는 매칭 키로 쓰지 않되, **키 집합(key set)만 sanity check로
  검증**한다: `sorted(recorded.args.keys()) == sorted(live.args.keys())`.
  값까지 비교하면 별도의 정규화 로직이 다시 필요해지므로(그 자체가
  버그/편향의 원천), 키 집합만 보는 선에서 구조적으로 다른 호출이
  우연히 같은 자리에 오는 사고만 잡는다.
- `source: model_client` 이벤트(§3의 `_observe` 산출물)는 **순번
  카운터(`seq`) 자체가 없고 매칭 대상에서 원천 제외**된다. 대신
  `parent_seq` 필드로 "이 모델 제안이 몇 번째 `tool_wrap` 이벤트에
  선행하는가"만 기록한다. 리플레이 엔진은 이 필드를 무시하고, 타임라인
  UI(§11) 렌더링에서만 순서 표시 목적으로 사용한다.

인자 → 구조적 특징 정규화(§7 featurize 단계의 sqlglot AST/path
정규화/shell 분해)는 이 매칭 규칙과 별개다. 그건 **규칙 평가**용이고,
여기서 정한 건 **리플레이 재생**용이다. 혼동하지 않는다.

## 7. 규칙 생성 엔진 (기둥 3의 심장)

### 규칙의 세 가지 출처

룰셋에 들어가는 규칙은 한 가지 경로로만 생기지 않는다. 세 계층이
있다.

1. **seed 규칙** — rein 메인테이너(팀)가 라이브러리에 기본 제공하는
   규칙
2. **개발자 설정 규칙** — 통합하는 개발자가 YAML로 직접 작성
3. **자동 합성 규칙** — 실패 이벤트에서 자동 생성 (아래 §7.1~ 파이프라인.
   **제품의 핵심 차별점은 이 세 번째 계층이다.**

### 두 원칙

1. 생성은 확률적이어도 되지만, **집행은 결정론적**이다. LLM으로 규칙
   초안을 뽑아도 한 번 동결되면 코드로만 강제된다.
2. 규칙 학습 = 검증 가능한 탐색 문제. 로그 전체를 회귀 스위트로
   재사용해, **양성(실패) 전부 차단 ∧ 음성(정상 호출) 0회귀** 중 가장
   일반적인 후보를 사람이 아니라 로그가 고른다. (이 지점이 정적
   블랙리스트와 갈리는 지점이다.)

### 3단계 생성 파이프라인

1. **특징 추출 (Featurize)** — 결정론적, 데모에서 실제 구현.
   리터럴을 구조적 특징으로 추상화한다. 예: SQL은 AST 파싱
   (`sqlglot`)으로 `{statement_type: DROP, target: users, class:
   DDL_DESTRUCTIVE}` 형태로 변환. path는 정규화+글롭 매칭, shell은
   명령/플래그 분해.

   **severity 분류 테이블**(featurize 산출물과 연동, §9 스키마의
   `outcome.severity` 값을 여기서 결정론적으로 계산한다):

   | 카테고리 | 예시 | severity |
   |---|---|---|
   | DDL_DESTRUCTIVE (DROP/TRUNCATE) | SQL | critical |
   | DML 파괴적 연산 (WHERE 없는 UPDATE/DELETE) | SQL | critical |
   | 파일 삭제/덮어쓰기 | path | critical |
   | 파일 읽기/조회 | path | info |
   | 외부 API 실패(재시도 가능) | tool | warning |
   | 스키마 검증 실패 | any | warning |
   | 정상 실행(에러 없음) | any | info |

   `severity`는 오직 `outcome`(결과가 얼마나 심각했는가)에서만
   계산되는 파생값이며, `verdict`(allow/deny/retry/approve, 무엇을
   했는가)를 입력으로 쓰지 않는다 — 두 필드는 서로 다른 질문에 답하는
   축으로 명확히 분리한다.

2. **후보 술어 합성 + 회귀 검증 (Synthesize & Verify)** — 결정론적,
   데모에서 실제 구현. "진화"의 본체.
   - 조합 폭발 방지를 위해 **계층적 빔 서치**를 쓴다: 특징을
     `tool_name → class → agent.role` 순서로만 추가(임의 순열 금지),
     **빔 폭 K = 8, 탐색 깊이 3단계**로 고정.
   - 검증 비용은 O(K·N) (K는 상수, N은 코퍼스 크기)로, 코퍼스에 대해
     선형이다. "지수항을 상수로 고정"하는 것이지 O(N)이 되는 게
     아니라는 점에 주의.
3. **LLM 사후분석 (Post-mortem, 폴백)** — 로드맵 항목, 데모 구현
   범위 아님. 정적 분석이 의도를 못 잡는 비정형 인자에서만 소형
   로컬 LLM이 DSL 초안을 제안하고, 그 초안도 반드시 2단계 회귀
   검증을 통과해야 동결된다.

### 콜드 스타트 (로그가 없을 때)

세 가지 안전장치: ① 골든 트레이스(`rein seed`로 정상 시나리오 녹화)
② 합성 음성(권한 테이블에서 정답 도출) ③ 신뢰도 게이팅(좁게 시작해
쌓이면 넓힘). 최악의 경우에도 "틀려도 안전한 방향"(과소차단 회피)으로
틀리게 설계한다.

## 8. 규칙 저장 형식 — provenance 박힌 YAML

```yaml
rule:
  id: rule_0007
  origin: auto              # 실패에서 자동 생성됨 (사람 X)
  when:
    tool: execute_sql
    features:
      class: { in: [DDL_DESTRUCTIVE] }
  scope:
    agent.role: content_editor
  then: deny
  rationale: "OWASP LLM06 Excessive Agency — content_editor는 파괴적 DDL 권한 없음"
  provenance:
    born_from: evt_0042              # 어느 실패가 이 규칙을 낳았는지 (양성)
    validated_against: golden_run.jsonl  # 음성 코퍼스(정상 호출)만. 양성은 born_from에
    blocks: [evt_0042]               # 막은 양성
    regressions: []                  # 깬 정상 호출 = 0
    generality_rank: 2/3
    extractor: sqlglot==27.x         # 특징을 뽑은 파서 버전
    tool_sig: "execute_sql:a1b2c3"   # 도구 시그니처 해시
    feature_schema: v3               # 특징 명칭 스키마 버전
```

**중요한 스코프 규칙**: `validated_against`는 정상(양성 아닌) 호출로만
채운다. 규칙을 낳은 타겟 실패 트레이스는 `born_from`에만 들어가야
하고 `validated_against`에 섞으면 안 된다 — positive/negative 분리가
흐려진다.

**stale 검증 게이트**: `extractor`/`tool_sig`/`feature_schema`를
메타데이터로 박고, 런타임 로드 시 현재 환경 값과 비교한다. 불일치하면
규칙을 즉시 적용하지 않고 stale로 표시한 뒤 재학습을 유도한다. 재학습은
별도 기능이 아니라 `born_from`에 적힌 원본 실패 이벤트를 파이프라인에
다시 태우는 것 = 기존 루프 재사용이다.

`blocks`/`regressions` 필드는 회귀 매트릭스 렌더링에 그대로 쓰이고,
`rationale`은 live-rerun에서 deny 사유로 환류된다. 한 YAML이 감사·
시각화·런타임 가이드 세 군데서 일한다는 점을 코드 설계에서 유지할 것.

## 9. 이벤트 로그 스키마 (확정)

```json
{
  "schema_version": "v1",
  "evt": "evt_0042",
  "seq": 42,
  "source": "tool_wrap",
  "parent_seq": null,
  "tool_name": "execute_sql",
  "args": { "query": "DROP TABLE users;" },
  "context": { "task": "공지사항 업데이트", "agent_role": "content_editor" },
  "verdict": "allow",
  "outcome": {
    "status": "error",
    "side_effect": "table_dropped",
    "severity": "critical",
    "detail": "DROP TABLE users during content_editor task"
  }
}
```

| 필드 | 의미 |
|---|---|
| `schema_version` | 이벤트 스키마 버전. §8 `rules.yaml`의 `feature_schema`와 대칭 — severity enum 등을 나중에 확장할 때 옛 로그와의 호환을 여기로 관리한다. |
| `seq` | 단일 순번 카운터. `source: tool_wrap` 이벤트에만 부여되며, 리플레이 매칭의 유일한 키다(§6). `model_client` 이벤트는 `null`. |
| `source` | `"tool_wrap"` \| `"model_client"`. `_intercept`/`_observe` 중 어느 경로에서 왔는지(§3). |
| `parent_seq` | `model_client` 이벤트 전용. 이 제안이 선행하는 `tool_wrap` 이벤트의 `seq`. 매칭에는 안 쓰고 타임라인 렌더링 전용(§6). |
| `outcome.severity` | `"info"` \| `"warning"` \| `"critical"` 고정 enum. 계산 규칙은 §7 분류 테이블. |
| `outcome.detail` | 자유 텍스트. severity만으로 안 잡히는 구체 사유(리포트에서 재조사 없이 바로 읽히도록). |

## 10. 기술 스택 — 자체 구현 vs 기성 라이브러리

차별점은 "규칙 합성 + 회귀 채점" 하나뿐이므로, 개발 시간의 80%는
거기에 쓴다. 나머지는 검증된 OSS로 조립한다.

**자체 구현 (얇게)**
- 미니멀 레코더 (VCR 패턴, vcrpy 미사용)
- 인터셉터/어댑터 표면 (= 공개 5줄 API)
- 가드레일 집행 엔진 (함수 리스트 + 첫 non-allow 승리)

**자체 구현 (핵심, 시간의 80%)**
- 규칙 합성 + 회귀 채점

**기성 라이브러리 사용**
| 용도 | 라이브러리 |
|---|---|
| 스키마 검증 + 룰 DSL | `pydantic v2` + `PyYAML` |
| SQL 특징 추출 | `sqlglot` (무의존성 파서) |
| path/shell 특징 추출 | `fnmatch` · `pathlib` · `shlex` (표준 라이브러리) |
| 프로바이더 비종속 LLM 호출 | `litellm` (선택 사항) |
| 이벤트 저장소 | `json` + append (`structlog` 선택 사항) |
| 정적 리포트 | `Jinja2` (로컬 대시보드는 M4에서 `Streamlit` 검토) |

**절대 쓰지 않는 것**: OPA, Cedar 등 외부 정책 엔진. `vcrpy`.

## 11. 데모/리포트 — 만들 때 지킬 것

- 산출물은 세 층: ① rein 라이브러리(실체) ② 산출 파일
  (`golden_run.jsonl`, `run.jsonl`, `rules.yaml`, 디스크) ③ 뷰어
  레이어(선택 — CLI 표 / 정적 HTML `report.html`(권장) / 로컬 서버
  대시보드는 M4로 미룸).
- 대회용 권장 조합은 "라이브러리 + 정적 `report.html`"이다. 서버를
  안 띄워서 발표 중 안 죽고, 심사위원이 파일을 직접 열어 재현할 수
  있다.
- 리포트 화면 하나에 4가지만 담는다: ① 분기 타임라인 ② before/after
  지표 ③ 후보 회귀 표(일반화가 옳은가) ④ 채택 규칙 회귀 매트릭스
  (기존 게 안 깨지는가). 반드시 실제 JSONL을 먹여 그려야 한다(연출
  의심 차단). 분기 타임라인은 §9의 `parent_seq`를 이용해 model_client
  제안 → tool_wrap 실행 순서를 표시한다.
- **UI 투자는 바벨(barbell) 전략**: 위 한 화면에만 투자, 나머지
  (설정 화면·인증·범용 타임라인 대시보드)는 투자 금지. 특히 "범용
  타임라인 UI"는 함정으로 명시되어 있다 — LangSmith/Phoenix가 이미
  더 잘 하고 스코프 크리프만 유발한다. 설정값은 UI가 아니라 YAML로
  받는다.

## 12. 마일스톤 (구현 순서 가이드)

| 단계 | 트랙 | 내용 |
|---|---|---|
| M1 | 대회 | 인터셉터 + 가드레일 4단계 + JSONL 로그 + record/replay-verify/live-rerun 3모드 + 골든 시드(`rein seed`). 공개 API 시그니처 확정 |
| M2 | 대회 | 실패→규칙(1·2단계)→리플레이 재검증 + 회귀 매트릭스 렌더 |
| M3 | OSS | README + 어댑터 2종 + 라이선스 + 예제 (API는 M1에서 고정, 신규 설계 없음) |
| M4 | OSS | 확장 버킷 — 규칙 생성 3단계(로컬 LLM 폴백), 추가 어댑터, 로컬 서버 대시보드, OWASP Top 10 추가 매핑, 비동기 도구 지원 검토. 임계 경로 밖 |

여유가 생기면 M4 중에서도 **추가 어댑터를 최우선**으로 한다(OSS 채택률에
직결). 3단계 LLM 폴백보다 우선.

## 13. 팀 구조 (코드 오너십 참고용)

- **서영** — 시스템 아키텍트 & 프로덕트 매니저(PM): 적은 투입 시간과
  가장 높은 프로젝트 이해도를 감안해 직접 구현량보다 고레버리지
  의사결정에 집중하는 역할로 확정됐다. 담당 R&R은 다음과 같다.
  - 전체 시스템 아키텍처 및 핵심 데이터 흐름(Data Flow) 설계
  - CLI 명령어 명세(`rein seed`, `rein replay`) 및 외부 API
    데코레이터 구조 정의
  - A/B 테스트 데모 시나리오(분기 로직) 설계 및 최종 산출물 검증
  - 공개 API 표면(5줄 통합) 설계·동결, 컴포넌트 간 인터페이스 계약
    정의(이벤트 JSONL 스키마, 어댑터 인터페이스, 규칙 YAML의
    `born_from`/`blocks`/`regressions`), PR 리뷰 및 스코프 가드
    ("범용 타임라인 대시보드 함정" 등 스코프 크리프 차단), 데모
    대본·심사 서사·포지셔닝 문서 작성
- **가희** — Core Engine Owner: 규칙 생성 엔진, 회귀 채점, 리플레이
  엔진
- **현준** — Infrastructure Owner: 인터셉터, 어댑터 레이어, 가드레일
  파이프라인, 이벤트 저장소
- **세림** — Validation Corpus / Demo Assets: 골든 트레이스, 데모
  시나리오, `report.html` 스켈레톤 (critical path 밖)

작업 순서 원칙: **서영이 API 표면/인터페이스 계약을 먼저 동결**하고,
그 다음 나머지 셋이 병렬로 작업한다. 현준의 이벤트 로그와 세림의
코퍼스가 가희의 엔진에 흘러 들어가는 구조다. 현준은 M1 이후 가희와
페어링해 가장 무거운 컴포넌트(엔진)의 리스크를 분산한다.

## 14. 코드/문서 작성 컨벤션

- Python 라이브러리(pip 배포), 필요 시 문서 빌드는 Node.js(`docx`
  npm 패키지) 사용.
- 한국어 산문 작성 시: "구현을 진행하였다" 류의 명사화 표현, 과도한
  줄표(—), 상투적 강조어를 피한다.
- 문서는 living file로 관리한다 — changelog 섹션이나 리비전 마커
  없이 수정 사항을 본문에 자연스럽게 반영한다.