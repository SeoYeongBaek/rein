"""규칙 생성 엔진 (CLAUDE.md §7). featurize -> synthesize & verify (계층적 빔서치 K=8, depth=3)
-> (로드맵) LLM post-mortem.

전제 조건(§7 featurize 의존): 이 모듈이 계산하는 severity/class는 SQL
featurizer(sqlglot 파싱) 결과에서만 나온다. rules.synthesize_rule과
rule_matches는 evt["args"]를 이 모듈의 featurize()로 다시 계산해서 쓰지,
로그에 이미 박혀 있는 evt["outcome"]["severity"] 문자열을 신뢰하지
않는다. 로그의 severity가 featurizer가 아닌 다른 경로(수기 태깅, 다른
버전의 분류 테이블 등)로 채워졌다면 값이 어긋날 수 있기 때문이다(§8
stale 검증 게이트와 동일한 우려).

이슈 #10 guard 구현됨: `rein rule-from`의 콜드 스타트 합성 음성 필터
(이슈 #11)도 같은 이유로 로그의 outcome.severity 필드를 직접 읽지 않는다
— cli.py의 `_recomputed_severity()`가 evt.args를 featurize()로 다시
계산하고 이 SEVERITY_TABLE로 severity를 도출해서 "info"인지 확인한다.
featurize가 실패하는(비-SQL) 이벤트는 검증 불가로 간주해 음성 후보에서
자동 제외된다 — 로그 기록 경로(§3 인터셉터, 현준 담당)가 이 테이블을
실제로 재사용해서 태깅하는지와 무관하게 항상 안전한 방향이다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sqlglot
import yaml
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

# §5.2 권한 테이블(role -> tool -> 허용 class 목록, rein.yaml `permissions:`)에서 각
# class를 대표하는 SQL 한 줄. permission_table_negatives가 이 표로 합성 음성의
# args["query"]를 fabricate한다 — 그냥 문자열 라벨이 아니라 featurize()가 실제로
# 같은 class를 재추출할 수 있는 진짜 SQL이어야 rule_matches 회귀 판정에 반영되기 때문이다.
CANONICAL_SQL_BY_CLASS: dict[str, str] = {
    "SQL_SAFE": "SELECT 1",
    "DDL_DESTRUCTIVE": "DROP TABLE synthetic_permission_check;",
    "DML_DESTRUCTIVE": "DELETE FROM synthetic_permission_check;",
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

    cli.py의 rule-from(synthesize/verify)과 replay --compare(_verdict_from_rules)가
    공유하는 유일한 매처다. evt["outcome"]["severity"]가 아니라 evt["args"]를
    다시 featurize한 결과를 쓴다 — 위 모듈 docstring의 featurizer 전제 조건 참고.
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


def load_permission_table(config_path: str | Path = "rein.yaml") -> dict[str, dict[str, list[str]]]:
    """rein.yaml의 `permissions:` 섹션을 role -> tool -> 허용 class 목록으로 읽는다
    (§5.2 권한 테이블, 이슈 #11 확정).

    ```yaml
    permissions:
      content_editor:
        execute_sql: [SQL_SAFE]
      admin:
        execute_sql: [SQL_SAFE, DDL_DESTRUCTIVE, DML_DESTRUCTIVE]
    ```

    §5 권한 체크 스테이지가 나중에 실제로 구현될 때도 같은 `permissions:` 키를 재사용할
    수 있도록 스키마를 맞춰 둔다 — 두 소비자가 각자 다른 키를 쓰면 드리프트 위험만
    생긴다(§8 stale 검증 게이트와 같은 우려). 파일이 없거나 `permissions` 키가 없으면
    빈 dict를 돌려준다: 이 테이블은 콜드 스타트 negatives에 신호를 "추가"할 뿐 필수
    입력이 아니므로, 없으면 그냥 log 기반 negatives만 쓰는 게 맞다(load_stage_order와
    달리 fail-closed 대상이 아니다 — 이 테이블이 없다고 규칙 생성 자체가 막히면 안 된다).
    """
    path = Path(config_path)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    table = data.get("permissions") or {}
    if not isinstance(table, dict):
        raise ValueError(f"{config_path}: permissions는 role -> tool -> class 목록 매핑이어야 함")
    for role, tools in table.items():
        _validate_permission_entry(config_path, role, tools)
    return table


def _validate_permission_entry(config_path: str | Path, role: str, tools: Any) -> None:
    """§5.2 fail-closed: role별 항목의 구조/class명을 즉시 검증한다 (이슈 #11).

    검증 없이 조용히 넘어가면(오타 class명이 CANONICAL_SQL_BY_CLASS.get()에서
    걸러지듯 그냥 사라지면) 결과는 "안전 실패"가 아니라 "안전 미실행"이다 —
    관리자가 admin에게 DDL_DESTRUCTIVE를 허용한다고 써놨는데 오타로 그 항목이
    조용히 사라지면, permission_table_negatives가 admin의 정당한 호출에 대한
    negative를 못 만들어내고 synthesize_rule은 그 신호가 아예 없었던 것처럼
    동작해 admin까지 막는 규칙을 "회귀 0건"으로 승인해버린다 — 관리자가 명시적으로
    지켜주려던 권한이 오타 하나로 조용히 뚫리는 셈이라, §5 stage_order의 조용한
    무시 금지 원칙(UnknownStageError)과 동일하게 여기서도 즉시 에러가 맞다.
    """
    if not isinstance(tools, dict):
        raise ValueError(
            f"{config_path}: permissions.{role}은 tool -> class 목록 매핑이어야 함 "
            f"(실제: {tools!r})"
        )
    for tool, classes in tools.items():
        if not isinstance(classes, list) or not all(isinstance(c, str) for c in classes):
            raise ValueError(
                f"{config_path}: permissions.{role}.{tool}은 문자열 class 목록이어야 함 "
                f"(실제: {classes!r})"
            )
        unknown = [c for c in classes if c not in CANONICAL_SQL_BY_CLASS]
        if unknown:
            raise ValueError(
                f"{config_path}: permissions.{role}.{tool}에 알 수 없는 class {unknown} "
                f"(허용값: {sorted(CANONICAL_SQL_BY_CLASS)})"
            )


def permission_table_negatives(
    born_from: dict[str, Any], permission_table: dict[str, dict[str, list[str]]]
) -> list[dict[str, Any]]:
    """§5.2 권한 테이블 기반 합성 음성 (콜드 스타트 안전장치 ②, 이슈 #11 확정).

    로그에 실제로 찍힌 호출만 훑는 `_cold_start_negatives`(log 기반, cli.py)와 달리,
    이 함수는 role -> tool -> 허용 class 목록이라는 선언적 사실 자체로 negative
    이벤트를 fabricate한다. 로그에 해당 role/class 조합의 호출이 아예 없어도(진짜
    콜드 스타트) depth 2/3 후보가 과대 차단인지 검증할 신호를 만들어내는 것이 목적이다
    — log 기반 방식만 쓰면 log에 없는 조합은 검증할 수가 없어 §7 "틀려도 안전한
    방향"에 따라 항상 depth=3(가장 좁은 scope)으로 수렴하는데, 실제로는 여러 role에
    안전한 class라도 이 신호가 없으면 절대 일반화되지 못한다.

    tool_name은 born_from과 같은 도구만 다룬다 — synthesize_rule의 후보들이 애초에
    when.tool을 born_from 도구로 고정해서, 다른 도구의 권한 항목은 rule_matches의
    when.tool 비교에서 곧바로 no-match 처리되어 아무 신호도 못 주기 때문이다.

    born_from과 정확히 같은 (role, tool, class) 조합은 제외한다 — 그 조합은 지금
    막으려는 실패 그 자체이므로, 권한 테이블에 실수로 같이 올라 있어도 negative로
    셀 수 없다(§8 "validated_against는 음성 전용, born_from과 섞지 않는다"와 같은 원칙).

    permission_table의 구조/class명은 load_permission_table을 거치지 않고 직접
    전달됐을 수도 있으므로 여기서도 다시 검증한다(_validate_permission_entry) —
    호출 경로와 무관하게 오타/잘못된 구조가 조용히 무시되지 않게 하기 위해서다.
    """
    tool_name = born_from.get("tool_name")
    role_at_fault = (born_from.get("context") or {}).get("agent_role")
    born_features = featurize(born_from.get("args") or {})
    born_class = born_features.get("class") if born_features else None

    negatives: list[dict[str, Any]] = []
    for role, tools in permission_table.items():
        _validate_permission_entry("<permission_table>", role, tools)
        allowed_classes = tools.get(tool_name)
        if not allowed_classes:
            continue
        for klass in allowed_classes:
            if role == role_at_fault and klass == born_class:
                continue
            sql = CANONICAL_SQL_BY_CLASS[klass]  # _validate_permission_entry가 클래스명을 이미 보장
            negatives.append(
                {
                    "evt": f"synthetic_perm_{role}_{tool_name}_{klass}",
                    "source": "synthetic_permission_table",
                    "tool_name": tool_name,
                    "args": {"query": sql},
                    "context": {"agent_role": role},
                    "verdict": "allow",
                }
            )
    return negatives


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

    negatives가 아예 비어 있으면("증거 0건") 가장 좁은(도달 가능한 가장 깊은)
    후보를 채택한다. "회귀가 안 남 = 그 depth가 안전하다고 검증됨"을
    전제하는데, negatives가 비어 있으면 어떤 depth를 골라도 회귀가 0건으로
    나와 얕은(넓은) depth부터 통과해버린다 — 이는 "검증됨"이 아니라 "검증할
    음성이 없었을 뿐"이다. 증거가 없는 상태에서 가장 넓게 일반화하면 §7
    "틀려도 안전한 방향으로" 원칙과 정반대(SELECT 같은 무해한 호출까지 막는
    과대차단)가 되므로, 이 경우만 별도로 가장 좁은 후보를 강제한다.

    반환값의 `candidate_trail`은 depth 1~3 후보 전부(채택된 depth 이후 것도
    포함)의 (when/scope/regressions)를 담는다 — §11 리포트 요소③(후보 회귀
    표: "가장 얕은 통과 depth가 왜 채택됐는지 보여줌")가 채택 depth 하나만으론
    설명이 안 되고, 얕은 depth들이 왜 탈락했는지(회귀 목록)까지 필요하기
    때문이다(#53). 회귀 판정 자체는 depth마다 독립적으로 다시 계산하므로
    이전처럼 "루프가 break를 못 타면 마지막 값이 남는" 부수효과에 기대지
    않는다.
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

    trail: list[dict[str, Any]] = [
        {
            "depth": candidate["depth"],
            "when": candidate["when"],
            "scope": candidate["scope"],
            "regressions": [neg["evt"] for neg in negatives if rule_matches(candidate, neg)],
        }
        for candidate in candidates
    ]

    # negatives가 비어 있으면("증거 0건") 모든 depth가 트리비얼하게 회귀
    # 0건이므로, 첫 번째(가장 얕은) 항목을 그대로 고르면 §7 원칙과 반대로
    # 가장 넓게 일반화해버린다 — 이 경우만 가장 좁은 항목을 강제한다.
    if negatives:
        chosen_entry = next((entry for entry in trail if not entry["regressions"]), trail[-1])
    else:
        chosen_entry = trail[-1]

    return {
        "when": chosen_entry["when"],
        "scope": chosen_entry["scope"],
        "then": "deny",
        "blocks": [born_from["evt"]],
        "regressions": chosen_entry["regressions"],
        "generality_rank": f"{chosen_entry['depth']}/3",
        "candidate_trail": trail,
    }
