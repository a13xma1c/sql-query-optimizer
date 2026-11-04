"""The whole pipeline against every engine: connect -> EXPLAIN -> parse -> analyze -> explain -> optimize -> re-EXPLAIN.

``engine_env`` is live for DuckDB and PostgreSQL (and for Trino/Snowflake when their URL is configured) and a
recorded server otherwise; the assertions are written so both paths prove the same contract.
"""

import json

import pytest
from sqlalchemy import create_engine, text

from sqlanalyzer.analyzer import analyze_query, compare_queries
from sqlanalyzer.db import ExplainError, fetch_plan_text, is_read_only
from sqlanalyzer.db import test_connection as check_connection
from sqlanalyzer.dialects import ENGINES, Engine
from sqlanalyzer.llm import LlmClient, LlmConfig, llm_optimize
from sqlanalyzer.optimizer import optimize_query, parse_schema
from sqlanalyzer.plans.diff import diff_plans

from .conftest import CLEAN_SQL, MESSY_SQL, SCHEMA_TEXT, EngineEnv

pytestmark = pytest.mark.e2e


def rows(env: EngineEnv, sql: str) -> list[tuple]:
    engine = create_engine(env.cfg.url())
    try:
        with engine.connect() as conn:
            return sorted(conn.execute(text(sql)).fetchall(), key=lambda r: tuple(str(v) for v in r))
    finally:
        engine.dispose()


# ------------------------------------------------------------------ connection
def test_connection_and_url(engine_env: EngineEnv):
    env = engine_env
    assert check_connection(env.cfg) == "Connection successful."
    assert env.cfg.display().startswith(ENGINES[env.engine].sa_driver.split("+")[0])
    if env.recorded is not None:
        assert env.recorded.urls[-1] == env.cfg.url().render_as_string(hide_password=False)
        assert env.recorded.statements[-1].strip().upper().startswith("SELECT 1")
    # the URL survives a round trip through ConnectionConfig.from_url (what the live env variables use)
    again = type(env.cfg).from_url(env.engine, env.cfg.url())
    assert again.display() == env.cfg.display() and again.database == env.cfg.database


# --------------------------------------------------------------- EXPLAIN pipeline
def test_explain_pipeline(engine_env: EngineEnv):
    env = engine_env
    plan_text = fetch_plan_text(env.cfg, MESSY_SQL)
    assert plan_text.strip()
    if env.recorded is not None:
        assert env.recorded.statements[-1].startswith(env.explain_prefix())
        assert MESSY_SQL.strip() in env.recorded.statements[-1]

    analysis = analyze_query(MESSY_SQL, env.engine, plan_text=plan_text)
    assert analysis.syntax_ok and analysis.plan is not None
    plan = analysis.plan
    assert plan.engine is env.engine and not plan.logical and not plan.analyzed
    assert analysis.effective_plan is plan and analysis.logical_plan is not None
    assert len(plan.nodes()) >= 2 and sorted(n.id for n in plan.nodes()) == list(range(len(plan.nodes())))
    assert any(n.relation for n in plan.nodes()), "at least one scan must name its table"
    assert any(n.kind == "join" for n in plan.nodes())
    # the explanation talks about the engine plan, not the offline one
    assert analysis.explanation is not None
    assert "no database was contacted" not in analysis.explanation.headline
    assert ENGINES[env.engine].label in analysis.explanation.headline
    assert len(analysis.explanation.steps) == len(plan.nodes())
    # static findings are still there, plan findings carry node ids
    static = {f.rule for f in analysis.findings if f.line is not None}
    assert {"implicit-join", "not-in-subquery", "non-sargable-predicate"} <= static
    for f in analysis.findings:
        if f.node_id is not None:
            assert f.node_id in plan.by_id()


def test_explain_analyze_is_limited_to_read_only_queries(engine_env: EngineEnv):
    env = engine_env
    dialect = ENGINES[env.engine].sqlglot_dialect
    assert is_read_only(MESSY_SQL, dialect) and not is_read_only("DELETE FROM orders WHERE id = 1", dialect)
    before = len(env.recorded.statements) if env.recorded is not None else None
    with pytest.raises(ExplainError, match="Only plain SELECT"):
        fetch_plan_text(env.cfg, "DELETE FROM orders WHERE id = 1", analyze=True)
    if env.recorded is not None:
        assert len(env.recorded.statements) == before  # refused before anything reached the server


def test_server_errors_become_explain_errors(engine_env: EngineEnv):
    with pytest.raises(ExplainError) as excinfo:
        fetch_plan_text(engine_env.cfg, "SELECT * FROM missing_table")
    assert "missing_table" in str(excinfo.value).lower()


# ------------------------------------------------------------- optimizer + engine
def test_rule_rewrite_runs_and_explains_on_the_engine(engine_env: EngineEnv):
    env = engine_env
    dialect = ENGINES[env.engine].sqlglot_dialect
    result = optimize_query(MESSY_SQL, dialect, schema=parse_schema(SCHEMA_TEXT).mapping())
    assert result.changed and {rw.rule for rw in result.rewrites} >= {"explicit-join", "sargable-date",
                                                                       "not-in-to-not-exists"}
    optimized = result.optimized_sql
    if env.live:
        assert rows(env, optimized) == rows(env, MESSY_SQL)  # same answer on the real engine
        assert rows(env, MESSY_SQL)  # the sample data does produce matches
    before = analyze_query(MESSY_SQL, env.engine, plan_text=fetch_plan_text(env.cfg, MESSY_SQL))
    after = analyze_query(optimized, env.engine, plan_text=fetch_plan_text(env.cfg, optimized))
    assert after.plan is not None and not after.plan.logical
    cmp = compare_queries(before, after)
    assert not cmp.offline_plans and cmp.plan_diff is not None
    assert {f.rule for f in cmp.resolved} >= {"implicit-join", "non-sargable-predicate", "not-in-subquery"}
    assert not any(f.severity.value == "error" for f in cmp.introduced)
    if env.live:
        assert not cmp.plan_diff.identical  # the engine really plans the rewrite differently


def test_llm_suggestion_is_validated_and_explained_on_the_engine(engine_env: EngineEnv):
    env = engine_env
    dialect = ENGINES[env.engine].sqlglot_dialect

    class StubClient(LlmClient):
        def complete(self, prompt, system=""):
            assert f"dialect: {dialect}" in prompt and "customers: id int" in prompt
            return json.dumps({"sql": CLEAN_SQL, "changes": [{"title": "anti join", "risk": "none"}], "notes": ""})

    client = StubClient(LlmConfig(model="stub"), transport=lambda *a: {})
    result = llm_optimize(MESSY_SQL, dialect, client, schema=parse_schema(SCHEMA_TEXT).mapping(),
                          schema_text=SCHEMA_TEXT)
    assert result.review is not None
    assert result.ok, [c.detail for c in result.review.failures]
    plan_text = fetch_plan_text(env.cfg, result.sql)
    analysis = analyze_query(result.sql, env.engine, plan_text=plan_text)
    assert analysis.plan is not None and analysis.explanation is not None
    if env.live:
        assert rows(env, result.sql) == rows(env, MESSY_SQL)


def test_plan_diff_of_two_versions_on_the_engine(engine_env: EngineEnv):
    env = engine_env
    a = fetch_plan_text(env.cfg, MESSY_SQL)
    b = fetch_plan_text(env.cfg, CLEAN_SQL)
    old = analyze_query(MESSY_SQL, env.engine, plan_text=a)
    new = analyze_query(CLEAN_SQL, env.engine, plan_text=b)
    assert old.plan is not None and new.plan is not None
    diff = diff_plans(old.plan, new.plan)
    assert diff.entries and any("Operators" in line for line in diff.summary)
    assert diff_plans(old.plan, old.plan).identical
    if env.live:
        assert not diff.identical


def test_catalog_introspection_feeds_the_schema_aware_analysis(engine_env: EngineEnv):
    """Importing the catalog makes join-key type checks, statistics and keys available to every analysis."""
    from sqlanalyzer.catalog import CatalogOptions, introspect
    from sqlanalyzer.optimizer import format_schema

    env = engine_env
    if not env.live:
        pytest.skip("catalog introspection needs a live server (recorded servers replay EXPLAIN only)")
    result = introspect(env.cfg, CatalogOptions(tables=["orders", "shipments", "customers", "events"]))
    hints = result.hints
    assert {"orders", "shipments", "customers"} <= set(hints.tables)
    assert hints.stats["orders"].rows and hints.stats["orders"].rows > 0
    assert "id" in hints.stats["orders"].primary and "ts" in hints.stats["orders"].indexed
    assert hints.tables["shipments"]["order_ref"].startswith(("VARCHAR", "CHARACTER"))
    if env.engine is Engine.POSTGRES:
        assert hints.stats["events"].partition == {"dt"}
        assert hints.stats["orders"].ndv.get("customer_id")  # from pg_stats
    text = format_schema(hints)
    assert parse_schema(text).tables == hints.tables
    analysis = analyze_query("SELECT o.id FROM orders o JOIN shipments s ON s.order_ref = o.id WHERE o.amount = '5'",
                             env.engine, schema=hints.mapping(), stats=hints.stats, optimize=False)
    rules = {f.rule for f in analysis.findings}
    assert {"join-type-mismatch", "predicate-type-mismatch"} <= rules
    assert analysis.cost is not None
    assert analysis.cost.table_rows["orders"] == hints.stats["orders"].rows
