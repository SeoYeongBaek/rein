"""규칙 생성 엔진 (CLAUDE.md §7). featurize -> synthesize & verify (계층적 빔서치 K=8, depth=3)
-> (로드맵) LLM post-mortem.

전제 조건(§7 featurize 의존): 이 모듈이 계산하는 severity/class는 SQL
featurizer(sqlglot 파싱) 결과에서만 나온다. rules.synthesize_rule과
rule_matches는 evt["args"]를 이 모듈의 featurize()로 다시 계산해서 쓰지,
로그에 이미 박혀 있는 evt["outcome"]["severity"] 문자열을 신뢰하지
않는다. 로그의 severity가 featurizer가 아닌 다른 경로(수기 태깅, 다른
버전의 분류 테이블 등)로 채워졌다면 값이 어긋날 수 있기 때문이다(§8
stale 검증 게이트와 동일한 우려). `rein rule-from`의 콜드 스타트 합성
음성 필터(이슈 #11, cli.py의 outcome.severity == "info" 체크)는 이
모듈과 별개로 로그의 severity 필드를 직접 읽으므로, 그 필터가 안전하려면
로그 기록 시점에 severity가 바로 이 SEVERITY_TABLE로 계산되어 있어야
한다. 아직 기록 경로(현준 담당, §3 인터셉터)가 이 테이블을 재사용하는지
보장되지 않는다. 어긋남이 확인되면 이슈 #10 구현 시 severity 소스를
검증하는 guard를 cli.py 쪽에 추가할 것.
"""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

# §9 schema_version과 대칭 — 특징 명칭이 바뀌면 이 버전을 올린다.
FEATURE_SCHEMA_VERSION = "v1"

# §7 severity 분류 테이블 중 SQL class -> severity 매핑만 다룬다(path/tool 축은
# featurizer 스코프 밖, §10). 지금 당장 이 상수를 쓰는 곳은 없다 — 로그 기록
# 시점(§3 인터셉터, 현준 담당)의 severity 태깅이 나중에 같은 테이블을
# 재사용하도록 미리 공개해서 두 곳의 분류 기준이 드리프트하지 않게 한다.
SEVERITY_TABLE: dict[str, str] = {
    "DDL_DESTRUCTIVE": "critical",
    "DML_DESTRUCTIVE": "critical",
    "SQL_SAFE": "info",
}

# class -> rationale에 박을 OWASP 태그(§8 예시 "OWASP LLM06 Excessive Agency" 재현용).
OWASP_TAGS: dict[str, str] = {
    "DDL_DESTRUCTIVE": "OWASP LLM06 Excessive Agency",
    "DML_DESTRUCTIVE": "OWASP LLM06 Excessive Agency",
    "SQL_SAFE": "",
}


def featurize(args: dict[str, Any]) -> dict[str, Any] | None:
    """SQL featurizer (§7, §10 — M1/M2 필수 featurizer는 SQL 하나뿐).

    args["query"]를 sqlglot으로 파싱해 {statement_type, target, class}를
    반환한다. SQL이 아니거나(query 키 없음) 파싱에 실패하면 None —
    비-SQL 도구는 아직 featurizer 지원 범위 밖이라는 뜻이다(§10).
    """
    query = args.get("query")
    if not isinstance(query, str):
        return None

    try:
        parsed = sqlglot.parse_one(query)
    except sqlglot.errors.ParseError:
        return None

    statement_type = type(parsed).__name__.upper()

    if isinstance(parsed, exp.Drop | exp.TruncateTable):
        klass = "DDL_DESTRUCTIVE"
    elif isinstance(parsed, exp.Delete | exp.Update) and parsed.args.get("where") is None:
        klass = "DML_DESTRUCTIVE"
    else:
        klass = "SQL_SAFE"

    target = _extract_target(parsed)
    return {"statement_type": statement_type, "target": target, "class": klass}


def _extract_target(parsed: exp.Expression) -> str | None:
    """DROP/TRUNCATE/DELETE/UPDATE의 대상 테이블명. 그 외 문형은 None
    (rule_matches/synthesize_rule은 class만 쓰고 target은 참고용)."""
    if isinstance(parsed, exp.TruncateTable):
        tables = parsed.args.get("expressions") or []
        return tables[0].name if tables else None
    table = parsed.this
    return getattr(table, "name", None)


def rule_matches(rule: dict[str, Any], evt: dict[str, Any]) -> bool:
    """when.tool + when.features.class + scope.agent.role 매칭.

    feat-9 브랜치의 _rule_matches와 같은 모양이되, features.class를
    featurize()로 실제 계산해서 비교하는 버전이다. evt["outcome"]["severity"]가
    아니라 evt["args"]를 다시 featurize한 결과를 쓴다 — 위 모듈 docstring의
    featurizer 전제 조건 참고.
    """
    when = rule.get("when", {})
    if when.get("tool") and when.get("tool") != evt.get("tool_name"):
        return False

    features = when.get("features") or {}
    class_spec = features.get("class")
    if class_spec is not None:
        allowed = class_spec.get("in", []) if isinstance(class_spec, dict) else [class_spec]
        evt_features = featurize(evt.get("args") or {})
        evt_class = evt_features.get("class") if evt_features else None
        if evt_class not in allowed:
            return False

    scope = rule.get("scope") or {}
    scoped_role = scope.get("agent.role")
    if scoped_role and scoped_role != (evt.get("context") or {}).get("agent_role"):
        return False

    return True


def _candidate(tool_name: str, klass: str | None, role: str | None, depth: int) -> dict[str, Any]:
    when: dict[str, Any] = {"tool": tool_name}
    scope: dict[str, Any] | None = None
    if depth >= 2 and klass is not None:
        when["features"] = {"class": {"in": [klass]}}
    if depth >= 3 and role is not None:
        scope = {"agent.role": role}
    return {"when": when, "scope": scope, "depth": depth}


def synthesize_rule(born_from: dict[str, Any], negatives: list[dict[str, Any]]) -> dict[str, Any]:
    """depth 1→2→3(tool / tool+class / tool+class+role) 순서로 후보를 만들어
    negatives(음성 코퍼스) 회귀를 세고, 회귀 0건인 가장 얕은 depth를 채택한다
    (§7 계층적 빔서치의 최소 구현 — K=8/깊이3 전체 빔서치가 아니라 이 세 depth만
    고정 탐색).

    depth 2는 born_from이 SQL(featurize 성공)일 때만, depth 3은 거기에
    agent_role까지 있을 때만 후보에 들어간다. depth=3(또는 그게 없으면
    도달 가능한 가장 깊은 depth)은 이론상 항상 회귀 0건이 보장된다 —
    born_from 자신과 완전히 같은 tool+class+role인 음성은 (evt != born_from
    필터 때문에) 존재할 수 없으므로 탐색은 항상 종료한다. 단, 이 보장은
    negatives가 §11 콜드 스타트 필터(같은 tool, severity=="info")를 거쳤거나
    --golden 코퍼스가 실제로 안전한 호출만 담고 있다는 전제 위에서만 성립한다.

    negatives가 아예 비어 있으면("증거 0건") depth 1→2→3 순회를 하지 않고
    바로 가장 좁은(도달 가능한 가장 깊은) 후보를 채택한다. 순회 방식은
    "회귀가 안 남 = 그 depth가 안전하다고 검증됨"을 전제하는데, negatives가
    비어 있으면 어떤 depth를 골라도 회귀가 0건으로 나와 얕은(넓은) depth부터
    통과해버린다 — 이는 "검증됨"이 아니라 "검증할 음성이 없었을 뿐"이다.
    증거가 없는 상태에서 가장 넓게 일반화하면 §7 "틀려도 안전한 방향으로"
    원칙과 정반대(SELECT 같은 무해한 호출까지 막는 과대차단)가 되므로,
    이 경우만 별도로 가장 좁은 후보를 강제한다.
    """
    tool_name = born_from["tool_name"]
    role = (born_from.get("context") or {}).get("agent_role")
    features = featurize(born_from.get("args") or {})
    klass = features.get("class") if features else None

    candidates = [_candidate(tool_name, klass, role, depth=1)]
    if klass is not None:
        candidates.append(_candidate(tool_name, klass, role, depth=2))
        if role is not None:
            candidates.append(_candidate(tool_name, klass, role, depth=3))

    chosen = candidates[-1]
    chosen_regressions: list[str] = []
    if negatives:
        # depth 1~3 전부 회귀가 나면 break를 못 타서 이 chosen_regressions(비어있지
        # 않음)가 그대로 반환된다 — 호출자(cli.py)가 이 경우를 실패로 처리해야
        # 하는데 아직 안 함(§7 "양성 전부 차단 ∧ 음성 0회귀" 위반 상태로 통과).
        chosen_regressions = [neg["evt"] for neg in negatives if rule_matches(chosen, neg)]
        for candidate in candidates:
            regressions = [neg["evt"] for neg in negatives if rule_matches(candidate, neg)]
            if not regressions:
                chosen = candidate
                chosen_regressions = []
                break

    return {
        "when": chosen["when"],
        "scope": chosen["scope"],
        "then": "deny",
        "blocks": [born_from["evt"]],
        "regressions": chosen_regressions,
        "generality_rank": f"{chosen['depth']}/3",
    }
