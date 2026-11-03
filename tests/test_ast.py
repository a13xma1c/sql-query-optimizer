"""Tests for the SQLGlot based abstract syntax tree model (sqlanalyzer.ast)."""

import json

import pytest
from sqlglot import exp

from sqlanalyzer.ast import (
    AstNode,
    ast_stats,
    ast_to_dict,
    ast_to_json,
    ast_to_text,
    build_ast,
    build_ast_graph,
    find_nodes,
    graphviz_available,
    node_at,
    node_label,
    parse_ast,
    render_ast_svg,
)
from sqlanalyzer.parsing import parse_sql

SQL = (
    "SELECT o.id, count(*) AS n\n"
    "FROM orders o JOIN customers c ON o.cid = c.id\n"
    "WHERE o.x = 'a' AND o.amount > 10\n"
    "GROUP BY o.id ORDER BY n DESC LIMIT 5"
)


@pytest.fixture
def tree() -> AstNode:
    trees, parsed = parse_ast(SQL, "postgres")
    assert parsed.ok and len(trees) == 1
    return trees[0]


def test_root_is_select_with_expected_clauses(tree):
    assert tree.kind == "Select" and tree.arg == "" and tree.depth == 0
    slots = {c.arg for c in tree.children}
    assert {"expressions[0]", "expressions[1]", "from_", "joins[0]", "where", "group", "order", "limit"} <= slots


def test_ids_are_preorder_and_unique(tree):
    ids = [n.id for n in tree.walk()]
    assert ids == list(range(len(ids)))
    assert tree.size == len(ids) and tree.height == ast_stats(tree).depth


def test_children_mirror_sqlglot_args(tree):
    for node in tree.walk():
        expected = 0
        for value in node.expression.args.values():
            if isinstance(value, exp.Expression):
                expected += 1
            elif isinstance(value, list):
                expected += sum(isinstance(v, exp.Expression) for v in value)
        assert len(node.children) == expected
        for child in node.children:
            assert child.depth == node.depth + 1
            assert child.expression.parent is node.expression


def test_labels():
    e = parse_sql("SELECT a.b AS c, 'x', 1, TRUE, NULL, CAST(d AS INT), lower(e), f + g FROM t u WHERE h = i OR NOT j",
                  "duckdb").first
    labels = {type(n).__name__: node_label(n) for n in e.walk()}
    assert labels["Column"] in ("a.b", "d", "e", "f", "g", "h", "i", "j")
    assert labels["Alias"] == "AS c"
    assert labels["Literal"] in ("'x'", "1")
    assert labels["Boolean"] == "TRUE" and labels["Null"] == "NULL"
    assert labels["Cast"] == "AS INT" and labels["Lower"] == "LOWER"
    assert labels["Add"] == "+" and labels["EQ"] == "=" and labels["Or"] == "OR" and labels["Not"] == "NOT"
    assert labels["TableAlias"] == "u"
    assert node_label(exp.Join(this=exp.to_table("t"), side="LEFT")) == "LEFT JOIN"
    assert node_label(exp.Select(distinct=exp.Distinct())) == "DISTINCT"


def test_positions_point_into_source(tree):
    where = next(c for c in tree.children if c.arg == "where")
    assert where.line == 3 and where.col == 7  # first operand "o.x" on line 3
    join = next(c for c in tree.children if c.arg == "joins[0]")
    assert join.line == 2
    lines = SQL.splitlines()
    for n in tree.walk():
        if n.kind == "Identifier":
            assert n.line is not None and n.start is not None
            assert SQL[n.start:n.end + 1] == n.label
            assert lines[n.line - 1][n.col - 1:].startswith(n.label)


def test_node_at_returns_innermost(tree):
    offset = SQL.index("amount")
    node = node_at(tree, offset)
    assert node is not None and node.kind == "Identifier" and node.label == "amount"
    assert node_at(tree, 10_000) is None
    # the enclosing comparison spans the identifier
    gt = next(n for n in tree.walk() if n.kind == "GT")
    assert gt.start <= offset <= gt.end


def test_find_nodes_and_stats(tree):
    assert len(find_nodes(tree, "column")) == 7  # o.id, o.cid, c.id, o.x, o.amount, o.id (group), n (order)
    assert len(find_nodes(tree, "Join")) == 1
    stats = ast_stats(tree)
    assert stats.nodes == tree.size and stats.leaves > 0 and stats.kinds["Select"] == 1
    assert stats.top_kinds[0][0] == "Identifier"


def test_text_rendering(tree):
    text = ast_to_text(tree)
    lines = text.splitlines()
    assert lines[0] == "Select"
    assert "  expressions[0]: Column o.id" in lines
    assert any(line.startswith("  where: Where") for line in lines)
    assert any(line.strip() == "this: Column o.x" for line in lines)
    with_sql = ast_to_text(tree, with_sql=True)
    assert "-- " in with_sql and "WHERE o.x = 'a'" in with_sql


def test_dict_and_json_round_trip(tree):
    d = ast_to_dict(tree)
    assert d["kind"] == "Select" and "arg" not in d and len(d["children"]) == len(tree.children)
    assert d["children"][0] == {"id": 1, "kind": "Column", "arg": "expressions[0]", "label": "o.id", "sql": "o.id",
                                "line": 1, "col": 8, "children": d["children"][0]["children"]}
    loaded = json.loads(ast_to_json(tree, include_sql=False))
    assert "sql" not in loaded and loaded["id"] == 0

    def count(n):
        return 1 + sum(count(c) for c in n.get("children", []))

    assert count(loaded) == tree.size


def test_multiple_statements_and_errors():
    trees, parsed = parse_ast("SELECT 1; DELETE FROM t WHERE a = 1", "postgres")
    assert parsed.ok and [t.kind for t in trees] == ["Select", "Delete"]
    trees, parsed = parse_ast("SELECT FROM WHERE", "postgres")
    assert not parsed.ok and trees == []


@pytest.mark.parametrize("dialect", ["postgres", "duckdb", "trino", "snowflake"])
def test_build_ast_in_every_dialect(dialect):
    e = parse_sql("SELECT a, b FROM t WHERE c IN (SELECT d FROM u) AND e BETWEEN 1 AND 2", dialect).first
    root = build_ast(e, dialect)
    kinds = {n.kind for n in root.walk()}
    assert {"Select", "Where", "In", "Between", "Subquery"} <= kinds
    assert root.group == "statement"
    assert {n.group for n in root.walk()} <= {"statement", "clause", "table", "column", "literal", "function",
                                              "operator", "other"}


def test_long_sql_is_truncated_in_labels():
    sql = "SELECT " + ", ".join(f"column_{i}" for i in range(40)) + " FROM t"
    root = build_ast(parse_sql(sql, "duckdb").first)
    assert len(root.sql) <= 80 and root.sql.endswith("…")


def test_graph_source_lists_nodes_and_edges(tree):
    g = build_ast_graph(tree)
    src = g.source
    assert src.count("->") == tree.size - 1
    assert "<B>Select</B>" in src and "label=where" in src
    small = build_ast_graph(tree, max_nodes=5).source
    assert "more nodes" in small and small.count("->") <= 4


@pytest.mark.skipif(not graphviz_available(), reason="Graphviz 'dot' not installed")
def test_render_svg(tree):
    svg = render_ast_svg(tree)
    assert svg.startswith(b"<?xml") and b"<svg" in svg
