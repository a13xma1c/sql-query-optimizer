"""Execute original and optimized queries on DuckDB and compare the results.

Every SAFE rewrite must return exactly the same rows (as a multiset); REVIEW
rewrites may differ only in the documented NULL / duplicate edge cases.
The data deliberately contains NULLs, duplicates and boundary timestamps.
"""

import duckdb
import pytest

from sqlanalyzer.optimizer import Safety, optimize_query

SCHEMA = {
    "orders": {"id": "int", "customer_id": "int", "order_date": "date", "status": "text", "amount": "int",
               "s": "text", "ts": "timestamp"},
    "customers": {"id": "int", "name": "text", "region": "text"},
    "refunds": {"order_id": "int", "amt": "int"},
}

SETUP = """
CREATE TABLE customers AS SELECT * FROM (VALUES
    (1, 'alice', 'EU'), (2, 'bob', 'US'), (3, 'carol', NULL), (4, NULL, 'EU'), (5, 'dave', 'EU')
) t(id, name, region);
CREATE TABLE orders AS SELECT * FROM (VALUES
    (10, 1, DATE '2024-01-15', 'a', 100, 'abcdef', TIMESTAMP '2024-01-15 10:00:00'),
    (11, 1, DATE '2024-01-16', 'b', 50,  'abx',    TIMESTAMP '2024-01-16 00:00:00'),
    (12, 2, DATE '2023-12-31', 'c', NULL, NULL,    TIMESTAMP '2023-12-31 23:59:59'),
    (13, NULL, DATE '2024-01-15', 'a', 5, 'zzz',   NULL),
    (14, 9, NULL, NULL, 7, 'abc',                  TIMESTAMP '2024-06-01 12:00:00'),
    (15, 3, DATE '2024-01-15', 'd', 100, 'ab',     TIMESTAMP '2024-01-15 23:59:59'),
    (16, 5, DATE '2025-01-01', 'a', 100, 'abc%',   TIMESTAMP '2025-01-01 00:00:00'),
    (17, 5, DATE '2024-12-31', 'b', 1,   'ABC',    TIMESTAMP '2024-12-31 23:59:59.999')
) t(id, customer_id, order_date, status, amount, s, ts);
CREATE TABLE refunds AS SELECT * FROM (VALUES (10, 5), (12, 1), (NULL, 3), (10, 2)) t(order_id, amt);
"""

SAFE_QUERIES = [
    # joins
    "SELECT o.id, c.name FROM orders o, customers c WHERE o.customer_id = c.id AND o.amount > 10",
    "SELECT o.id FROM orders o, customers c, refunds r WHERE o.customer_id = c.id AND r.order_id = o.id AND r.amt > 1",
    "SELECT o.id, c.name FROM orders o, customers c WHERE o.amount = 100",
    "SELECT o.id FROM orders o LEFT JOIN customers c ON o.customer_id = c.id WHERE c.region = 'EU'",
    "SELECT o.id FROM orders o LEFT JOIN customers c ON o.customer_id = c.id WHERE c.region IS NOT NULL",
    "SELECT o.id FROM orders o LEFT JOIN customers c ON o.customer_id = c.id WHERE c.region IS NULL",
    "SELECT o.id FROM orders o LEFT JOIN customers c ON o.customer_id = c.id WHERE COALESCE(c.region, 'x') = 'x'",
    "SELECT o.id FROM orders o LEFT JOIN customers c ON o.customer_id = c.id WHERE c.id IN (1, 2) OR o.amount = 7",
    "SELECT o.id, c.name FROM orders o LEFT JOIN customers c ON o.customer_id = c.id LEFT JOIN refunds r ON r.order_id = o.id WHERE r.amt > 1",
    "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id WHERE c.id = o.customer_id AND c.region = 'EU'",
    "SELECT o.id FROM orders o CROSS JOIN customers c WHERE o.customer_id = c.id",
    "SELECT o.id FROM orders o LEFT JOIN (SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id) x ON x.customer_id = o.customer_id",
    # sargable dates
    "SELECT id FROM orders WHERE DATE(ts) = '2024-01-15'",
    "SELECT id FROM orders WHERE CAST(ts AS DATE) >= '2024-01-15' AND CAST(ts AS DATE) <= '2024-01-16'",
    "SELECT id FROM orders WHERE DATE(ts) > '2024-01-15'",
    "SELECT id FROM orders WHERE DATE(ts) < '2024-01-15'",
    "SELECT id FROM orders WHERE DATE(ts) BETWEEN '2024-01-15' AND '2024-12-31'",
    "SELECT id FROM orders WHERE YEAR(ts) = 2024",
    "SELECT id FROM orders WHERE YEAR(order_date) = 2024",
    "SELECT id FROM orders WHERE EXTRACT(YEAR FROM order_date) < 2024",
    "SELECT id FROM orders WHERE DATE(order_date) = '2024-01-15'",
    "SELECT id FROM orders WHERE '2024-12-31' < DATE(ts) OR YEAR(ts) <= 2023",
    "SELECT id FROM orders WHERE DATE_TRUNC('day', ts) = TIMESTAMP '2024-01-15 00:00:00'",
    # predicates
    "SELECT id FROM orders WHERE status = 'a' OR status = 'b' OR status IN ('c') OR amount = 7",
    "SELECT id FROM orders WHERE status IN ('a')",
    "SELECT id FROM orders WHERE status NOT IN ('a')",
    "SELECT id FROM orders WHERE s LIKE 'abc'",
    "SELECT id FROM orders WHERE s LIKE 'abc%'",
    "SELECT id FROM orders WHERE SUBSTR(s, 1, 3) = 'abc'",
    "SELECT id FROM orders WHERE LEFT(s, 2) = 'ab'",
    "SELECT id FROM orders WHERE SUBSTR(s, 1, 4) = 'abc%'",
    "SELECT id FROM orders WHERE 1 = 1 AND amount + 0 > 10 AND NOT (status = 'a')",
    "SELECT id FROM orders WHERE amount BETWEEN 5 AND 50 AND (status = 'a' OR status = 'b')",
    "SELECT id FROM orders WHERE NOT (status = 'a' OR status = 'b')",
    "SELECT id FROM orders WHERE status = 'a' AND (amount = 100 OR amount = 50 OR status = 'b')",
    "SELECT id FROM orders WHERE (status = 'a' AND amount = 100) OR (status = 'a' AND amount = 5)",
    # grouping
    "SELECT status, COUNT(*) AS n FROM orders GROUP BY status HAVING status <> 'a' AND COUNT(*) > 0",
    "SELECT status, SUM(amount) FROM orders GROUP BY status HAVING status IS NOT NULL",
    "SELECT DISTINCT status, COUNT(*) AS n FROM orders GROUP BY status",
    "SELECT DISTINCT status FROM orders GROUP BY status, amount",
    # subqueries / CTEs
    "SELECT id FROM orders o WHERE EXISTS (SELECT * FROM refunds r WHERE r.order_id = o.id)",
    "SELECT id FROM orders o WHERE o.customer_id IN (SELECT DISTINCT id FROM customers WHERE region = 'EU')",
    "WITH unused AS (SELECT 1 AS z), u2 AS (SELECT z FROM unused) SELECT id FROM orders",
    "WITH o AS (SELECT id, amount, customer_id, status FROM orders) SELECT o.id FROM o JOIN customers c ON o.customer_id = c.id WHERE o.status = 'a'",
    "SELECT s.id FROM (SELECT id, amount FROM orders WHERE amount > 5) s WHERE s.id > 10",
    "SELECT a.id FROM (SELECT id FROM orders WHERE status = 'a') a JOIN (SELECT id FROM orders WHERE status = 'a') b ON a.id = b.id",
    "SELECT c.name, (SELECT MAX(o.amount) FROM orders o WHERE o.customer_id = c.id) FROM customers c WHERE c.region = 'EU'",
    "SELECT o.id, (SELECT COUNT(*) FROM refunds r WHERE r.order_id = o.id) FROM orders o WHERE o.status = 'a' OR o.status = 'b'",
    "SELECT x.id FROM (SELECT id FROM orders WHERE amount > 5 ORDER BY amount LIMIT 3) x",
    # star expansion (schema run only)
    "SELECT * FROM orders WHERE status = 'a' OR status = 'b'",
    "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id WHERE DATE(o.ts) = '2024-01-15'",
    # DML-ish shapes that still are queries
    "SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id OR o.amount = c.id",
    "SELECT id FROM orders UNION ALL SELECT order_id FROM refunds",
    "SELECT id FROM orders WHERE id IN (SELECT order_id FROM refunds UNION SELECT id FROM customers)",
]

# (query, rules whose REVIEW rewrite is expected to change the result on this data)
REVIEW_QUERIES = [
    ("SELECT id FROM orders UNION SELECT order_id FROM refunds", {"union-all"}),
    ("SELECT id FROM orders o WHERE o.id NOT IN (SELECT r.order_id FROM refunds r)", {"not-in-to-not-exists"}),
    ("SELECT id FROM orders WHERE amount = NULL", {"null-comparison"}),
    ("SELECT id FROM orders WHERE amount <> NULL", {"null-comparison"}),
]


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    c.execute(SETUP)
    yield c
    c.close()


def rows(con, sql):
    result = con.execute(sql).fetchall()
    return sorted(result, key=lambda r: tuple((v is None, str(v)) for v in r))


@pytest.mark.parametrize("sql", SAFE_QUERIES)
@pytest.mark.parametrize("with_schema", [False, True], ids=["no-schema", "schema"])
def test_safe_rewrites_preserve_results(con, sql, with_schema):
    expected = rows(con, sql)
    result = optimize_query(sql, "duckdb", schema=SCHEMA if with_schema else None, include_review=False)
    assert not result.errors, result.errors
    assert all(rw.safety is Safety.SAFE for rw in result.rewrites)
    if result.changed:
        assert rows(con, result.optimized_sql) == expected, result.optimized_sql


@pytest.mark.parametrize("sql", SAFE_QUERIES)
def test_review_rewrites_on_safe_corpus_preserve_results(con, sql):
    """With review rules on, this corpus still yields identical rows (its data has no NOT IN / UNION traps)."""
    expected = rows(con, sql)
    result = optimize_query(sql, "duckdb", schema=SCHEMA, include_review=True)
    assert not result.errors, result.errors
    if result.changed:
        assert rows(con, result.optimized_sql) == expected, result.optimized_sql


@pytest.mark.parametrize("sql, culprits", REVIEW_QUERIES)
def test_review_rewrites_change_results_only_as_documented(con, sql, culprits):
    result = optimize_query(sql, "duckdb", schema=SCHEMA, include_review=True)
    assert result.changed
    applied_review = {rw.rule for rw in result.rewrites if rw.safety is Safety.REVIEW}
    assert applied_review == culprits
    assert rows(con, result.optimized_sql) != rows(con, sql)
    safe_only = optimize_query(sql, "duckdb", schema=SCHEMA, include_review=False)
    if safe_only.changed:
        assert rows(con, safe_only.optimized_sql) == rows(con, sql)


def test_not_exists_rewrite_keeps_rows_when_subquery_has_nulls(con):
    sql = "SELECT id FROM orders o WHERE o.id NOT IN (SELECT r.order_id FROM refunds r)"
    assert rows(con, sql) == []  # the NULL order_id makes NOT IN return nothing
    result = optimize_query(sql, "duckdb", include_review=True)
    assert [r[0] for r in rows(con, result.optimized_sql)] == [11, 13, 14, 15, 16, 17]


def test_optimized_queries_produce_the_same_column_names(con):
    for sql in ("SELECT id, amount AS total, COUNT(*) FROM orders GROUP BY id, amount HAVING id > 0 AND id = id",
                "SELECT o.id, c.name FROM orders o, customers c WHERE o.customer_id = c.id"):
        result = optimize_query(sql, "duckdb", schema=SCHEMA)
        assert result.changed
        before = [d[0] for d in con.execute(sql).description]
        after = [d[0] for d in con.execute(result.optimized_sql).description]
        assert before == after
