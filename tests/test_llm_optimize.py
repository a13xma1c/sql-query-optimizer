"""End-to-end: ask a (stubbed) local model, validate, and compare with the rule engine."""

import json

import duckdb
import pytest

from sqlanalyzer.llm import LlmClient, LlmConfig, LlmError, llm_optimize
from sqlanalyzer.optimizer import optimize_query

SQL = ("SELECT o.id, c.name\n"
       "FROM orders o, customers c\n"
       "WHERE o.customer_id = c.id\n"
       "  AND DATE(o.ts) = '2024-01-15'\n"
       "  AND o.id NOT IN (SELECT r.order_id FROM refunds r)\n")
SCHEMA = {"orders": {"id": "int", "customer_id": "int", "ts": "timestamp", "status": "text"},
          "customers": {"id": "int", "name": "text"}, "refunds": {"order_id": "int"}}

GOOD_SQL = ("SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id "
            "WHERE o.ts >= DATE '2024-01-15' AND o.ts < DATE '2024-01-16' "
            "AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)")


def answer(sql: str, changes=(), notes: str = "") -> str:
    return json.dumps({"sql": sql, "changes": list(changes), "notes": notes})


class StubClient(LlmClient):
    """A client that never touches the network."""

    def __init__(self, reply, config: LlmConfig | None = None):
        super().__init__(config or LlmConfig(model="stub"), transport=lambda *a: {})
        self.reply = reply
        self.prompts: list[tuple[str, str]] = []

    def complete(self, prompt: str, system: str = "") -> str:
        self.prompts.append((prompt, system))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


# ------------------------------------------------------------------ happy path
def test_validated_suggestion_is_applicable():
    client = StubClient(answer(GOOD_SQL, [{"title": "sargable date", "reason": "index", "risk": "none"},
                                          {"title": "anti join", "reason": "faster", "risk": "result-may-change"}],
                               "check the NULL semantics"))
    result = llm_optimize(SQL, "postgres", client, schema=SCHEMA, schema_text="orders: id int")
    assert result.ok and result.applicable and not result.error
    assert result.review is not None and result.review.verdict == "needs review"  # the declared risk
    assert result.sql.startswith("SELECT") and "\n" in result.sql  # pretty printed
    assert result.suggestion.change_titles() == ["sargable date", "anti join"]
    assert {f.rule for f in result.review.resolved} >= {"implicit-join", "not-in-subquery"}
    assert result.seconds >= 0 and "stub" in result.summary()
    prompt, system = client.prompts[0]
    assert "senior SQL performance engineer" in system
    assert "orders: id int" in prompt and "rule engine produced this rewrite" in prompt


def test_rule_result_is_reused_and_compared():
    rules = optimize_query(SQL, "postgres", schema=SCHEMA)
    client = StubClient(answer(rules.optimized_sql))
    result = llm_optimize(SQL, "postgres", client, schema=SCHEMA, rules=rules)
    assert result.rules is rules
    assert result.review.matches_rules and result.ok
    assert "matches the rule engine" in result.summary()


def test_rules_are_computed_when_not_supplied():
    client = StubClient(answer(GOOD_SQL))
    result = llm_optimize(SQL, "postgres", client)
    assert result.rules is not None and result.rules.changed
    assert "rule engine produced this rewrite" in client.prompts[0][0]


# --------------------------------------------------------------------- failures
def test_rejected_rewrite_is_not_applicable():
    bad = "SELECT o.id, c.name FROM orders o JOIN customer_dim c ON o.customer_id = c.id"
    result = llm_optimize(SQL, "postgres", StubClient(answer(bad)))
    assert result.sql and not result.ok and not result.applicable
    assert result.review.verdict == "rejected"
    assert any("customer_dim" in c.detail for c in result.review.failures)


def test_transport_failure_is_reported():
    result = llm_optimize(SQL, "postgres", StubClient(LlmError("Cannot reach http://localhost:11434")))
    assert not result.ok and "Cannot reach" in result.error and result.summary().startswith("Local model failed")
    assert result.review is None and result.sql == ""


def test_unexpected_exception_is_contained():
    result = llm_optimize(SQL, "postgres", StubClient(ValueError("kaboom")))
    assert not result.ok and "ValueError: kaboom" in result.error


def test_answer_without_sql_is_reported():
    result = llm_optimize(SQL, "postgres", StubClient("I'm afraid I can't do that."))
    assert not result.ok and "No SQL" in result.error and result.suggestion is not None


def test_invalid_original_query_is_refused_before_calling_the_model():
    client = StubClient(answer("SELECT 1"))
    result = llm_optimize("SELECT FROM WHERE", "postgres", client)
    assert not result.ok and "does not parse" in result.error and client.prompts == []


def test_unparseable_suggestion_fails_validation():
    result = llm_optimize(SQL, "postgres", StubClient(answer("SELECT FROM WHERE")))
    assert not result.ok and result.review is not None
    assert result.review.failures[0].name == "syntax"


def test_non_json_answer_still_yields_sql():
    result = llm_optimize(SQL, "duckdb", StubClient(f"Here you go:\n```sql\n{GOOD_SQL}\n```"))
    assert result.sql and result.suggestion.parse_error
    assert result.review is not None and result.ok


@pytest.mark.parametrize("dialect", ["postgres", "duckdb", "trino", "snowflake"])
def test_works_in_every_dialect(dialect):
    result = llm_optimize(SQL, dialect, StubClient(answer(GOOD_SQL)), schema=SCHEMA)
    assert result.ok and result.dialect == dialect
    assert f"Engine / SQL dialect: {dialect}." in result.prompt


# ---------------------------------------------- the validated rewrite really runs
def test_accepted_rewrite_returns_the_same_rows_on_duckdb():
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE customers AS SELECT * FROM (VALUES (1, 'alice'), (2, 'bob'), (3, NULL)) t(id, name);
        CREATE TABLE orders AS SELECT * FROM (VALUES
            (10, 1, TIMESTAMP '2024-01-15 10:00:00', 'a'),
            (11, 2, TIMESTAMP '2024-01-15 23:59:59', 'b'),
            (12, 1, TIMESTAMP '2024-01-16 00:00:00', 'c'),
            (13, 3, NULL, 'd')) t(id, customer_id, ts, status);
        CREATE TABLE refunds AS SELECT * FROM (VALUES (11,), (99,)) t(order_id);
    """)
    original = ("SELECT o.id, c.name FROM orders o, customers c WHERE o.customer_id = c.id "
                "AND DATE(o.ts) = '2024-01-15' AND o.id NOT IN (SELECT r.order_id FROM refunds r)")
    result = llm_optimize(original, "duckdb", StubClient(answer(GOOD_SQL)), schema=SCHEMA)
    assert result.ok
    rows = lambda sql: sorted(con.execute(sql).fetchall(), key=str)  # noqa: E731
    # NOT IN -> NOT EXISTS is the only review-level change and refunds has no NULLs here
    assert rows(result.sql) == rows(original)
    con.close()
