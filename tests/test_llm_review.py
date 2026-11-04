"""Validation of an LLM rewrite against the original query and the rule engine."""

import pytest

from sqlanalyzer.llm import FAIL, PASS, WARN, review_llm_sql
from sqlanalyzer.optimizer import optimize_query

ORIGINAL = ("SELECT o.id, c.name FROM orders o, customers c "
            "WHERE o.customer_id = c.id AND DATE(o.ts) = '2024-01-15' AND o.status = 'a'")
GOOD = ("SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id "
        "WHERE o.ts >= DATE '2024-01-15' AND o.ts < DATE '2024-01-16' AND o.status = 'a'")
SCHEMA = {"orders": {"id": "int", "customer_id": "int", "ts": "timestamp", "status": "text"},
          "customers": {"id": "int", "name": "text"}}


def review(llm_sql, original=ORIGINAL, dialect="postgres", **kwargs):
    return review_llm_sql(original, llm_sql, dialect, **kwargs)


def status_of(result, name):
    return next(c.status for c in result.checks if c.name == name)


def names(result):
    return [c.name for c in result.checks]


# ------------------------------------------------------------------- happy path
def test_good_rewrite_is_accepted():
    result = review(GOOD, schema=SCHEMA)
    assert result.ok and result.verdict in ("validated", "needs review")
    assert status_of(result, "syntax") == PASS
    assert status_of(result, "statements") == PASS
    assert status_of(result, "sources") == PASS
    assert status_of(result, "projection") == PASS
    assert status_of(result, "query shape") == PASS
    assert status_of(result, "changed anything") == PASS
    assert {f.rule for f in result.resolved} >= {"implicit-join", "non-sargable-predicate"}
    assert not result.introduced
    assert "checks passed" in result.summary()
    assert result.statements and len(result.statements) == 1


def test_rewrite_matching_the_rule_engine_is_corroborated():
    rules = optimize_query(ORIGINAL, "postgres", schema=SCHEMA)
    result = review(rules.optimized_sql, rules=rules, schema=SCHEMA)
    assert result.matches_rules and status_of(result, "rule engine") == PASS
    assert "matches the rule engine" in result.summary()
    assert result.ok


def test_missed_rule_opportunities_are_reported():
    lazy = ("SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id "
            "WHERE DATE(o.ts) = '2024-01-15' AND o.status = 'a'")
    result = review(lazy, schema=SCHEMA)
    assert status_of(result, "rule engine") == WARN
    assert "sargable-date" in result.missed_rules and result.ok


# ------------------------------------------------------------------- hard fails
def test_syntax_error_is_fatal_and_stops_the_review():
    result = review("SELECT FROM WHERE")
    assert not result.ok and result.verdict == "rejected"
    assert names(result) == ["syntax"] and status_of(result, "syntax") == FAIL
    assert "does not parse" in result.failures[0].detail


def test_hallucinated_table_is_rejected():
    result = review("SELECT o.id, c.name FROM orders o JOIN customer_dim c ON o.customer_id = c.id "
                    "WHERE o.status = 'a' AND o.ts >= DATE '2024-01-15' AND o.ts < DATE '2024-01-16'")
    assert not result.ok and status_of(result, "sources") == FAIL
    assert "customer_dim" in result.failures[0].detail


def test_dropped_predicate_is_rejected():
    result = review("SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id")
    assert not result.ok and status_of(result, "predicates") == FAIL
    assert "disappeared" in next(c.detail for c in result.checks if c.name == "predicates")


def test_changed_projection_is_rejected():
    result = review("SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id "
                    "WHERE o.status = 'a' AND o.ts >= DATE '2024-01-15' AND o.ts < DATE '2024-01-16'")
    assert not result.ok and status_of(result, "projection") == FAIL
    assert "1 column(s) instead of 2" in next(c.detail for c in result.checks if c.name == "projection")


def test_statement_count_or_kind_change_is_rejected():
    assert status_of(review(GOOD + "; SELECT 1"), "statements") == FAIL
    assert status_of(review("DELETE FROM orders WHERE status = 'a'", "SELECT id FROM orders WHERE status = 'a'"),
                     "statements") == FAIL


def test_dropped_aggregation_distinct_or_limit_is_rejected():
    original = "SELECT DISTINCT status, count(*) AS n FROM orders GROUP BY status ORDER BY 1 LIMIT 5"
    # the rule engine removes the redundant DISTINCT itself, so the same reduction is only a warning
    redundant = review("SELECT status, count(*) AS n FROM orders GROUP BY status ORDER BY 1 LIMIT 5", original)
    assert status_of(redundant, "query shape") == WARN and redundant.ok
    assert "rule engine performs the same reduction" in next(
        c.detail for c in redundant.checks if c.name == "query shape")
    dropped = review("SELECT DISTINCT status, count(*) AS n FROM orders GROUP BY status ORDER BY 1", original)
    assert status_of(dropped, "query shape") == FAIL and "LIMIT" in dropped.failures[0].detail
    no_agg = review("SELECT DISTINCT status, 0 AS n FROM orders GROUP BY status ORDER BY 1 LIMIT 5", original)
    assert status_of(no_agg, "query shape") == FAIL
    assert "aggregates 1 -> 0" in next(c.detail for c in no_agg.checks if c.name == "query shape")


def test_introduced_error_finding_is_rejected():
    result = review("SELECT o.id, c.name FROM orders o, customers c "
                    "WHERE DATE(o.ts) = '2024-01-15' AND o.status = 'a' AND o.customer_id = o.customer_id")
    assert not result.ok
    assert status_of(result, "static analysis") == FAIL
    assert "cartesian" in result.failures[0].detail.lower()


# ----------------------------------------------------------------------- warnings
def test_unchanged_answer_is_flagged():
    result = review(ORIGINAL)
    assert status_of(result, "changed anything") == WARN and result.ok
    assert result.verdict == "needs review"


def test_dropped_table_warns():
    original = "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id WHERE o.status = 'a'"
    result = review("SELECT o.id FROM orders o WHERE o.status = 'a'", original)
    assert status_of(result, "sources") == WARN and "customers" in next(
        c.detail for c in result.checks if c.name == "sources")


def test_renamed_output_columns_warn():
    result = review("SELECT o.id AS order_id, c.name AS customer FROM orders o "
                    "JOIN customers c ON o.customer_id = c.id "
                    "WHERE o.status = 'a' AND o.ts >= DATE '2024-01-15' AND o.ts < DATE '2024-01-16'")
    assert status_of(result, "projection") == WARN and result.ok


def test_declared_risk_is_surfaced():
    result = review(GOOD, schema=SCHEMA, declared_changes=[{"title": "NOT IN -> NOT EXISTS", "risk": "result-may-change"},
                                                           {"title": "explicit join", "risk": "none"}])
    check = next(c for c in result.checks if c.name == "declared-risk")
    assert check.status == WARN and "NOT IN -> NOT EXISTS" in check.detail and result.ok
    assert all(c.name != "declared-risk" for c in review(GOOD, schema=SCHEMA).checks)


# ------------------------------------------------------------- star expansion
def test_star_expansion_without_schema_is_rejected():
    result = review("SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id",
                    "SELECT * FROM orders o, customers c WHERE o.customer_id = c.id")
    assert status_of(result, "projection") == FAIL
    assert "cannot be verified" in result.failures[0].detail


def test_star_expansion_is_checked_against_the_schema():
    original = "SELECT * FROM orders o, customers c WHERE o.customer_id = c.id"
    full = ("SELECT o.id, o.customer_id, o.ts, o.status, c.id, c.name FROM orders o "
            "JOIN customers c ON o.customer_id = c.id")
    assert status_of(review(full, original, schema=SCHEMA), "projection") == PASS
    partial = "SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id"
    result = review(partial, original, schema=SCHEMA)
    assert status_of(result, "projection") == FAIL and "expands to 6" in result.failures[0].detail


@pytest.mark.parametrize("dialect", ["postgres", "duckdb", "trino", "snowflake"])
def test_review_runs_in_every_dialect(dialect):
    result = review(GOOD, dialect=dialect, schema=SCHEMA)
    assert result.ok and len(result.checks) >= 8
