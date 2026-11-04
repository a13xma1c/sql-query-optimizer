"""Schema-aware static rules: unknown/ambiguous names, join-key and literal type checks, wide stars."""

import pytest

from sqlanalyzer.analyzer import analyze_query
from sqlanalyzer.dialects import Engine
from sqlanalyzer.models import Category, Severity
from sqlanalyzer.optimizer import parse_schema
from sqlanalyzer.parsing import parse_sql
from sqlanalyzer.static_schema import SchemaIndex, analyze_schema_aware, compatible, type_family

HINTS = parse_schema("""
orders (rows=2000000): id int pk, customer_id int, ts timestamp, status varchar, amount decimal(10,2)
customers (rows=50000): id int pk, name text, status varchar, region text, signup_date date
shipments (rows=150000): id int pk, order_ref varchar, shipped_at timestamp
refunds (rows=5000): order_id int indexed, amt int
""")
WIDE = parse_schema("wide: " + ", ".join(f"c{i} int" for i in range(25)) + "\nnarrow: a int, b int")


def findings(sql: str, hints=HINTS, dialect: str = "postgres"):
    parsed = parse_sql(sql, dialect)
    assert parsed.ok, parsed.errors
    return {f.rule: f for f in analyze_schema_aware(parsed.statements, hints.mapping(), hints.stats)}


def all_findings(sql: str, hints=HINTS, dialect: str = "postgres"):
    return analyze_schema_aware(parse_sql(sql, dialect).statements, hints.mapping(), hints.stats)


# ------------------------------------------------------------------ type families
@pytest.mark.parametrize("typ, family", [
    ("int", "integer"), ("BIGINT", "integer"), ("NUMBER(38,0)", "integer"), ("serial", "integer"),
    ("decimal(10,2)", "decimal"), ("NUMBER(10,2)", "decimal"), ("double precision", "float"),
    ("timestamp without time zone", "timestamp"), ("TIMESTAMP_NTZ", "timestamp"), ("date", "date"),
    ("character varying(20)", "text"), ("TEXT", "text"), ("uuid", "uuid"), ("jsonb", "complex"),
    ("VARIANT", "complex"), ("int[]", "complex"), ("bytea", "binary"), ("boolean", "bool"),
    ("geometry", "other"), ("UNKNOWN", None), ("", None), (None, None),
])
def test_type_family(typ, family):
    assert type_family(typ) == family


def test_compatibility():
    assert compatible("integer", "integer") == "ok" and compatible("integer", "decimal") == "soft"
    assert compatible("integer", "float") == "soft" and compatible("date", "timestamp") == "soft"
    assert compatible("integer", "text") == "mismatch" and compatible("uuid", "text") == "mismatch"
    assert compatible("integer", None) == "ok" and compatible("other", "text") == "ok"


# --------------------------------------------------------------------- names
def test_unknown_table_and_column():
    f = findings("SELECT o.id, o.nope FROM orders o JOIN missing m ON m.id = o.id")
    assert f["unknown-table"].severity is Severity.WARNING and "'missing'" in f["unknown-table"].message
    assert "o.nope" in f["unknown-column"].message and "5 columns declared" in f["unknown-column"].message
    assert f["unknown-column"].category is Category.SCHEMA and f["unknown-column"].line == 1


def test_ambiguous_and_unresolvable_unqualified_columns():
    f = findings("SELECT name, status, nope FROM orders o JOIN customers c ON c.id = o.customer_id")
    assert "o, c" in f["ambiguous-column"].message and "'status'" in f["ambiguous-column"].message
    assert "'nope'" in f["unknown-column"].message
    assert "ambiguous-column" not in findings("SELECT name FROM orders o JOIN customers c ON c.id = o.customer_id")
    # single-table queries never report ambiguity and output aliases are not unknown columns
    assert findings("SELECT o.amount AS total FROM orders o WHERE total > 1") == {}


def test_unknown_alias_and_correlated_references():
    f = findings("SELECT bogus.x FROM orders o")
    assert "'bogus'" in f["unknown-alias"].message
    assert findings("SELECT c.id FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)") == {}


def test_ctes_derived_tables_and_partial_schemas_do_not_trigger_unknown_names():
    assert findings("WITH r AS (SELECT order_id FROM refunds) SELECT r.order_id FROM r") == {}
    assert findings("SELECT d.x FROM (SELECT id AS x FROM orders) d") == {}
    # one unknown table: unqualified columns cannot be judged, so only the table is reported
    f = findings("SELECT whatever FROM orders o JOIN mystery m ON m.id = o.id")
    assert set(f) == {"unknown-table"}
    assert all_findings("SELECT * FROM t", parse_schema("")) == []


# ---------------------------------------------------------------------- types
def test_join_type_mismatch():
    f = findings("SELECT 1 FROM orders o JOIN shipments s ON s.order_ref = o.id")
    assert f["join-type-mismatch"].severity is Severity.WARNING
    assert "s.order_ref is VARCHAR" in f["join-type-mismatch"].message and "o.id is INT" in f["join-type-mismatch"].message
    soft = findings("SELECT 1 FROM orders o JOIN customers c ON c.signup_date = o.ts")
    assert soft["join-type-mismatch"].severity is Severity.INFO
    assert "join-type-mismatch" not in findings("SELECT 1 FROM orders o JOIN customers c ON c.id = o.customer_id")
    # also in WHERE-based joins and regardless of operand order
    assert "join-type-mismatch" in findings("SELECT 1 FROM orders o, shipments s WHERE o.id = s.order_ref")


def test_predicate_type_mismatch():
    f = findings("SELECT 1 FROM orders o WHERE o.amount = '5' AND o.status = 42 AND o.id IN (1, '2')")
    messages = [x.message for x in all_findings(
        "SELECT 1 FROM orders o WHERE o.amount = '5' AND o.status = 42 AND o.id IN (1, '2')")]
    assert f["predicate-type-mismatch"].severity is Severity.WARNING
    assert any("'o.amount' is DECIMAL" in m and "text literal" in m for m in messages)
    assert any("'o.status' is VARCHAR" in m and "integer literal" in m for m in messages)
    assert any("'o.id' is INT" in m for m in messages)


@pytest.mark.parametrize("predicate", [
    "o.ts = '2024-01-01'", "o.ts >= '2024-01-01 10:00:00'", "o.status = '2024-01-01'", "o.amount = 5",
    "o.amount = 5.5", "o.id = 1.5", "o.status LIKE 'a%'", "o.ts BETWEEN DATE '2024-01-01' AND DATE '2024-02-01'",
    "o.amount = CAST('5' AS DECIMAL)", "o.status IS NULL", "o.id = o.customer_id",
])
def test_predicates_that_are_fine(predicate):
    assert "predicate-type-mismatch" not in findings(f"SELECT 1 FROM orders o WHERE {predicate}")


def test_join_on_unindexed_large_table():
    f = findings("SELECT 1 FROM orders o JOIN customers c ON c.id = o.customer_id")
    assert f["join-on-unindexed-column"].severity is Severity.INFO and "o.customer_id" in f["join-on-unindexed-column"].message
    assert "join-on-unindexed-column" not in findings("SELECT 1 FROM refunds r JOIN orders o ON o.id = r.order_id")
    small = parse_schema("a (rows=10): id int\nb (rows=10): a_id int")
    assert "join-on-unindexed-column" not in findings("SELECT 1 FROM a JOIN b ON b.a_id = a.id", small)


# --------------------------------------------------------------------- stars
def test_wide_select_star():
    f = findings("SELECT * FROM wide w WHERE w.c1 = 1", WIDE)
    assert f["wide-select-star"].severity is Severity.WARNING and "25 columns" in f["wide-select-star"].message
    assert "wide-select-star" not in findings("SELECT * FROM narrow n", WIDE)
    f = findings("SELECT w.*, n.a FROM wide w JOIN narrow n ON n.a = w.c1", WIDE)
    assert "25 columns from w" in f["wide-select-star"].message
    f = findings("SELECT * FROM wide w JOIN narrow n ON n.a = w.c1", WIDE)
    assert "27 columns from w, n" in f["wide-select-star"].message


# ------------------------------------------------------------- schema index
def test_schema_index_handles_nested_mappings_and_bare_names():
    nested = {"sales": {"orders": {"id": "int"}, "refunds": {"order_id": "int"}}, "misc": {"orders": {"id": "int"}}}
    index = SchemaIndex.build(nested, {"sales.orders": HINTS.stats["orders"]})
    from sqlglot import exp

    assert index.lookup(exp.to_table("sales.orders"))[0] == "sales.orders"
    assert index.lookup(exp.to_table("refunds"))[0] == "sales.refunds"
    assert index.lookup(exp.to_table("orders")) is None  # ambiguous across schemas
    assert index.stats_for("sales.orders").rows == 2_000_000 and index.stats_for("orders").rows == 2_000_000
    assert SchemaIndex.build(None).empty


# ------------------------------------------------------------- integration
def test_analysis_includes_schema_findings_only_with_a_schema():
    sql = "SELECT o.id FROM orders o JOIN shipments s ON s.order_ref = o.id"
    with_schema = analyze_query(sql, Engine.DUCKDB, schema=HINTS.mapping(), stats=HINTS.stats, optimize=False)
    assert "join-type-mismatch" in {f.rule for f in with_schema.by_category(Category.SCHEMA)}
    without = analyze_query(sql, Engine.DUCKDB, optimize=False)
    assert without.by_category(Category.SCHEMA) == []
