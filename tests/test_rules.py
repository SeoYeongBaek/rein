"""규칙 생성 엔진 테스트 (CLAUDE.md §7, 이슈 #10)."""

from __future__ import annotations

from typing import Any

import pytest

from rein.rules import (
    featurize,
    load_permission_table,
    permission_table_negatives,
    rule_matches,
    synthesize_rule,
)


def _evt(
    evt: str,
    tool_name: str,
    query: str,
    verdict: str = "allow",
    severity: str = "info",
    role: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "evt": evt,
        "seq": int(evt.split("_")[-1]),
        "source": "tool_wrap",
        "tool_name": tool_name,
        "args": {"query": query},
        "context": {"agent_role": role} if role is not None else {},
        "verdict": verdict,
        "outcome": {"status": "ok", "severity": severity, "detail": ""},
    }


# ── featurize ────────────────────────────────────────────────────────────────


def test_featurize_drop_is_ddl_destructive():
    f = featurize({"query": "DROP TABLE users;"})
    assert f is not None
    assert f["class"] == "DDL_DESTRUCTIVE"
    assert f["target"] == "users"


def test_featurize_truncate_is_ddl_destructive():
    f = featurize({"query": "TRUNCATE TABLE users;"})
    assert f is not None
    assert f["class"] == "DDL_DESTRUCTIVE"
    assert f["target"] == "users"


def test_featurize_delete_without_where_is_dml_destructive():
    f = featurize({"query": "DELETE FROM users;"})
    assert f is not None
    assert f["class"] == "DML_DESTRUCTIVE"


def test_featurize_update_without_where_is_dml_destructive():
    f = featurize({"query": "UPDATE users SET active = 0;"})
    assert f is not None
    assert f["class"] == "DML_DESTRUCTIVE"


def test_featurize_delete_with_where_is_sql_safe():
    f = featurize({"query": "DELETE FROM users WHERE id = 1;"})
    assert f is not None
    assert f["class"] == "SQL_SAFE"


def test_featurize_update_with_where_is_sql_safe():
    f = featurize({"query": "UPDATE users SET active = 0 WHERE id = 1;"})
    assert f is not None
    assert f["class"] == "SQL_SAFE"


def test_featurize_select_is_sql_safe():
    f = featurize({"query": "SELECT * FROM users;"})
    assert f is not None
    assert f["class"] == "SQL_SAFE"


def test_featurize_non_sql_args_returns_none():
    assert featurize({"path": "/tmp/x"}) is None


def test_featurize_unparseable_query_returns_none():
    assert featurize({"query": "not valid sql ((("}) is None


# ── synthesize_rule ───────────────────────────────────────────────────────────


def test_synthesize_rule_no_negatives_picks_narrowest_depth():
    """음성이 아예 없으면("증거 0건") 일반화하지 않고 도달 가능한 가장 좁은
    depth(여기선 tool+class+role 모두 있으니 depth3)를 채택한다 — §7 "틀려도
    안전한 방향으로" 원칙. 회귀 0건은 depth1부터 전부 참이므로, 얕은 depth부터
    통과시키면 증거 없이 가장 넓게(과대차단 방향으로) 일반화하는 정반대 결과가
    나온다."""
    born_from = _evt("evt_0042", "execute_sql", "DROP TABLE users;", role="content_editor")
    rule = synthesize_rule(born_from, negatives=[])

    assert rule["generality_rank"] == "3/3"
    assert rule["when"]["features"]["class"]["in"] == ["DDL_DESTRUCTIVE"]
    assert rule["scope"] == {"agent.role": "content_editor"}
    assert rule["regressions"] == []
    assert rule["blocks"] == ["evt_0042"]


def test_synthesize_rule_same_role_safe_queries_picks_depth2():
    """같은 role의 안전 쿼리(SELECT)만 음성이면 depth1(tool만)은 회귀가
    나서(같은 도구의 모든 호출을 막으므로) depth2(tool+class)로 좁혀진다."""
    born_from = _evt("evt_0042", "execute_sql", "DROP TABLE users;", role="content_editor")
    negatives = [
        _evt("evt_0001", "execute_sql", "SELECT * FROM posts;", role="content_editor"),
        _evt("evt_0002", "execute_sql", "SELECT * FROM comments;", role="content_editor"),
    ]

    rule = synthesize_rule(born_from, negatives)

    assert rule["generality_rank"] == "2/3"
    assert rule["scope"] is None
    assert rule["when"]["features"]["class"]["in"] == ["DDL_DESTRUCTIVE"]
    assert rule["regressions"] == []


def test_synthesize_rule_other_role_same_class_picks_depth3():
    """다른 role이 같은 class(DDL_DESTRUCTIVE)로 정상 호출한 음성이 있으면
    depth2(tool+class)까지는 그 음성과도 매칭돼 회귀가 나서, role까지
    좁힌 depth3으로 채택된다."""
    born_from = _evt("evt_0042", "execute_sql", "DROP TABLE users;", role="content_editor")
    negatives = [
        _evt("evt_0001", "execute_sql", "DROP TABLE tmp_scratch;", role="dba"),
    ]

    rule = synthesize_rule(born_from, negatives)

    assert rule["generality_rank"] == "3/3"
    assert rule["scope"] == {"agent.role": "content_editor"}
    assert rule["regressions"] == []


def test_synthesize_rule_non_sql_born_from_only_has_depth1():
    """featurize가 실패하는 도구(비-SQL)는 class가 없어 depth1만 후보에 든다."""
    born_from = {
        "evt": "evt_0099",
        "seq": 99,
        "source": "tool_wrap",
        "tool_name": "delete_file",
        "args": {"path": "/tmp/x"},
        "context": {"agent_role": "content_editor"},
        "verdict": "allow",
        "outcome": {"status": "ok", "severity": "critical", "detail": ""},
    }

    rule = synthesize_rule(born_from, negatives=[])

    assert rule["generality_rank"] == "1/3"
    assert rule["when"] == {"tool": "delete_file"}
    assert rule["scope"] is None


# ── permission_table_negatives / load_permission_table (§5.2, 이슈 #11) ───────


def test_permission_table_negatives_fabricates_per_role_per_class():
    """role별 허용 class마다 대표 SQL로 합성 음성 하나씩 나온다. born_from과
    같은 (role, class) 조합(content_editor의 DDL_DESTRUCTIVE)은 제외된다."""
    born_from = _evt("evt_0042", "execute_sql", "DROP TABLE users;", role="content_editor")
    table = {
        "content_editor": {"execute_sql": ["SQL_SAFE"]},
        "admin": {"execute_sql": ["SQL_SAFE", "DDL_DESTRUCTIVE", "DML_DESTRUCTIVE"]},
    }

    negatives = permission_table_negatives(born_from, table)

    by_role_class = {(n["context"]["agent_role"], featurize(n["args"])["class"]) for n in negatives}
    assert ("content_editor", "SQL_SAFE") in by_role_class
    assert ("admin", "SQL_SAFE") in by_role_class
    assert ("admin", "DDL_DESTRUCTIVE") in by_role_class
    assert ("admin", "DML_DESTRUCTIVE") in by_role_class
    assert ("content_editor", "DDL_DESTRUCTIVE") not in by_role_class
    assert all(n["tool_name"] == "execute_sql" and n["verdict"] == "allow" for n in negatives)


def test_permission_table_negatives_ignores_other_tools():
    """born_from과 다른 tool_name에 대한 권한 항목은 무시된다 — synthesize_rule의
    후보가 when.tool을 born_from 도구로 고정해서 아무 신호도 못 주기 때문."""
    born_from = _evt("evt_0042", "execute_sql", "DROP TABLE users;", role="content_editor")
    table = {"admin": {"delete_file": ["SQL_SAFE"]}}

    assert permission_table_negatives(born_from, table) == []


def test_permission_table_negatives_lets_synthesize_rule_generalize_without_log_evidence():
    """log에 다른 호출이 전혀 없어도(negatives=[] 상황), 권한 테이블 기반 합성 음성만
    으로 depth2까지 안전하게 일반화된다 — 순수 log 기반이었다면 depth3(가장 좁은
    scope)에 그쳤을 상황(test_synthesize_rule_no_negatives_picks_narrowest_depth
    참고)."""
    born_from = _evt("evt_0042", "execute_sql", "DROP TABLE users;", role="content_editor")
    table = {"content_editor": {"execute_sql": ["SQL_SAFE"]}}

    negatives = permission_table_negatives(born_from, table)
    rule = synthesize_rule(born_from, negatives)

    assert rule["generality_rank"] == "2/3"
    assert rule["scope"] is None
    assert rule["regressions"] == []


def test_load_permission_table_missing_file_returns_empty(tmp_path):
    assert load_permission_table(tmp_path / "no_such_rein.yaml") == {}


def test_load_permission_table_missing_key_returns_empty(tmp_path):
    config = tmp_path / "rein.yaml"
    config.write_text("stage_order: [schema, permission, budget, safety]\n", encoding="utf-8")

    assert load_permission_table(config) == {}


def test_load_permission_table_reads_permissions_section(tmp_path):
    config = tmp_path / "rein.yaml"
    config.write_text(
        "permissions:\n"
        "  content_editor:\n"
        "    execute_sql: [SQL_SAFE]\n"
        "  admin:\n"
        "    execute_sql: [SQL_SAFE, DDL_DESTRUCTIVE, DML_DESTRUCTIVE]\n",
        encoding="utf-8",
    )

    table = load_permission_table(config)

    assert table["content_editor"]["execute_sql"] == ["SQL_SAFE"]
    assert table["admin"]["execute_sql"] == ["SQL_SAFE", "DDL_DESTRUCTIVE", "DML_DESTRUCTIVE"]


def test_load_permission_table_rejects_typo_class_name(tmp_path):
    """오타난 class명(SQL_SAFEE)을 조용히 무시하지 않고 즉시 에러 — §5 stage_order의
    조용한 무시 금지 원칙과 동일. 조용히 넘어가면 admin에게 주려던 예외가 사라진 채로
    synthesize_rule이 "회귀 0건"으로 오판해 admin까지 막는 규칙을 승인해버린다."""
    config = tmp_path / "rein.yaml"
    config.write_text(
        "permissions:\n  admin:\n    execute_sql: [SQL_SAFEE]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="SQL_SAFEE"):
        load_permission_table(config)


def test_load_permission_table_rejects_non_dict_role_entry(tmp_path):
    """role 값이 tool -> class 매핑(dict)이 아니면 조용히 넘기지 않고 즉시 에러."""
    config = tmp_path / "rein.yaml"
    config.write_text(
        "permissions:\n  admin: execute_sql\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="admin"):
        load_permission_table(config)


def test_permission_table_negatives_rejects_unknown_class_even_without_load():
    """load_permission_table을 거치지 않고 permission_table_negatives에 직접 잘못된
    class명을 넘겨도 조용히 스킵되지 않고 에러여야 한다 — 검증은 호출 경로에
    의존하면 안 된다."""
    born_from = {
        "evt": "evt_0042",
        "tool_name": "execute_sql",
        "args": {"query": "DROP TABLE users;"},
        "context": {"agent_role": "content_editor"},
        "verdict": "allow",
    }
    table = {"admin": {"execute_sql": ["DDL_DESTRUCTIV"]}}  # 오타: 마지막 E 누락

    with pytest.raises(ValueError, match="DDL_DESTRUCTIV"):
        permission_table_negatives(born_from, table)


def test_permission_table_negatives_rejects_non_dict_tools_even_without_load():
    born_from = {
        "evt": "evt_0042",
        "tool_name": "execute_sql",
        "args": {"query": "DROP TABLE users;"},
        "context": {"agent_role": "content_editor"},
        "verdict": "allow",
    }
    table = {"admin": ["execute_sql"]}  # dict가 아니라 list

    with pytest.raises(ValueError, match="admin"):
        permission_table_negatives(born_from, table)


# ── candidate_trail (§11 요소③ 후보 회귀 표 데이터, 이슈 #53) ─────────────────


def test_candidate_trail_shows_why_shallow_depths_were_rejected():
    """depth3이 채택돼도 candidate_trail에는 depth1(다른 role 회귀 포함)/depth2가
    왜 탈락했는지(회귀 evt 목록)가 그대로 남아 있어야 한다 — 채택된 depth
    하나만으로는 '가장 얕은 통과 depth가 왜 채택됐는지'를 설명할 수 없다."""
    born_from = _evt("evt_0042", "execute_sql", "DROP TABLE users;", role="content_editor")
    negatives = [
        _evt("evt_0001", "execute_sql", "SELECT * FROM posts;", role="content_editor"),
        _evt("evt_0002", "execute_sql", "DROP TABLE tmp_scratch;", role="dba"),
    ]

    rule = synthesize_rule(born_from, negatives)

    trail = {entry["depth"]: entry for entry in rule["candidate_trail"]}
    assert set(trail) == {1, 2, 3}
    assert trail[1]["regressions"] == ["evt_0001", "evt_0002"]
    assert trail[2]["regressions"] == ["evt_0002"]
    assert trail[3]["regressions"] == []
    assert rule["generality_rank"] == "3/3"


def test_candidate_trail_all_depths_regress_keeps_full_trail():
    """depth 1~3 전부 회귀가 나는 경우(cli.py의 fail-closed 게이트가 나중에
    이 값을 보고 거절)에도 candidate_trail은 3개 항목 모두를 담아야
    회귀 원인을 감사할 수 있다."""
    born_from = _evt("evt_0042", "execute_sql", "DROP TABLE users;", role="content_editor")
    negatives = [
        _evt("evt_0001", "execute_sql", "DROP TABLE tmp_scratch;", role="content_editor"),
    ]

    rule = synthesize_rule(born_from, negatives)

    trail = {entry["depth"]: entry for entry in rule["candidate_trail"]}
    assert set(trail) == {1, 2, 3}
    assert trail[3]["regressions"] == ["evt_0001"]
    assert rule["regressions"] == ["evt_0001"]
    assert rule["generality_rank"] == "3/3"
    assert rule["blocks"] == ["evt_0042"]  # 회귀가 남아도 born_from 자신은 항상 막는다


# ── blocks (하드코딩이 아니라 rule_matches로 검증해서 산출, 완료 기준 재정합) ──


def test_blocks_is_verified_via_rule_matches_not_hardcoded():
    """blocks는 `[born_from["evt"]]`를 그냥 박아넣는 게 아니라, 채택된 규칙의
    when/scope로 born_from을 rule_matches에 실제로 통과시켜서 얻은 값과
    같아야 한다 — regressions가 negatives를 rule_matches로 검증하는 것과
    같은 방식을 born_from(양성 1건)에도 적용한다."""
    born_from = _evt("evt_0042", "execute_sql", "DROP TABLE users;", role="content_editor")
    negatives = [
        _evt("evt_0001", "execute_sql", "SELECT * FROM posts;", role="content_editor"),
    ]

    rule = synthesize_rule(born_from, negatives)

    positive_rule = {"when": rule["when"], "scope": rule["scope"]}
    assert rule_matches(positive_rule, born_from)
    assert rule["blocks"] == ([born_from["evt"]] if rule_matches(positive_rule, born_from) else [])


def test_blocks_holds_for_non_sql_depth1_only_candidate():
    """featurize가 실패하는 도구(depth1만 후보에 듦)에서도 blocks가 born_from을
    실제로 매칭해서 채워진다."""
    born_from = {
        "evt": "evt_0099",
        "seq": 99,
        "source": "tool_wrap",
        "tool_name": "delete_file",
        "args": {"path": "/tmp/x"},
        "context": {"agent_role": "content_editor"},
        "verdict": "allow",
        "outcome": {"status": "ok", "severity": "critical", "detail": ""},
    }

    rule = synthesize_rule(born_from, negatives=[])

    assert rule["blocks"] == ["evt_0099"]


def test_candidate_trail_empty_negatives_reports_zero_regressions_for_all_depths():
    """negatives=[]("증거 0건")이어도 candidate_trail 자체는 각 depth의
    (트리비얼한) 0회귀를 보여준다 — 채택은 여전히 가장 좁은 depth로
    강제되지만(§7 원칙), trail 데이터는 실제 계산 결과를 숨기지 않는다."""
    born_from = _evt("evt_0042", "execute_sql", "DROP TABLE users;", role="content_editor")

    rule = synthesize_rule(born_from, negatives=[])

    assert [entry["regressions"] for entry in rule["candidate_trail"]] == [[], [], []]
    assert rule["generality_rank"] == "3/3"
