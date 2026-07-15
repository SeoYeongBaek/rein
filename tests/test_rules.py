"""규칙 생성 엔진 테스트 (CLAUDE.md §7, 이슈 #10)."""

from __future__ import annotations

from typing import Any

from rein.rules import (
    featurize,
    load_permission_table,
    permission_table_negatives,
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
