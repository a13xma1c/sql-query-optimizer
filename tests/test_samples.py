"""Every bundled sample is analyzed on every engine it lists; expectations come from the file headers.

Samples marked ``demo: yes`` are also executed on a (scaled-down) demo database: the original and the
rule-engine rewrite must return the same rows unless a review-level rule was applied.
"""

import duckdb
import pytest

from sqlanalyzer.analyzer import analyze_query
from sqlanalyzer.demo import PARTITION_HINTS, SCHEMA_HINTS, populate_duckdb
from sqlanalyzer.dialects import ENGINES, Engine
from sqlanalyzer.optimizer import Safety, parse_schema
from sqlanalyzer.parsing import parse_sql
from sqlanalyzer.partition import parse_hints
from sqlanalyzer.samples import get_sample, load_samples, parse_sample

SAMPLES = load_samples()
HINTS = parse_schema(SCHEMA_HINTS)
PARTITIONS = parse_hints(PARTITION_HINTS)
CASES = [(s, e) for s in SAMPLES for e in s.engines]


def test_corpus_is_well_formed():
    assert len(SAMPLES) >= 30
    keys = [s.key for s in SAMPLES]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    for s in SAMPLES:
        assert s.name and s.description and s.sql.strip().upper().startswith(("SELECT", "WITH"))
        assert s.engines and (s.expect or s.rewrites or s.expect_none), s.key
        assert not (s.expect_none and (s.expect or s.rewrites)), s.key
    assert get_sample("01_comma_join_nonsargable").expect == ("implicit-join", "non-sargable-predicate",
                                                              "not-in-subquery", "select-star")
    with pytest.raises(KeyError):
        get_sample("nope")


def test_header_parser():
    s = parse_sample("-- name: X\n-- engines: trino , snowflake\n-- expect: a, b\n-- rewrites: c\n-- demo: no\n"
                     "-- schema: yes\n\nSELECT 1\n", "k")
    assert s.name == "X" and s.engines == (Engine.TRINO, Engine.SNOWFLAKE) and s.expect == ("a", "b")
    assert s.rewrites == ("c",) and not s.demo and s.needs_schema and s.sql == "SELECT 1\n"
    plain = parse_sample("SELECT 2", "p")
    assert plain.engines == tuple(Engine) and plain.demo and plain.name == "p"


@pytest.mark.parametrize("sample, engine", CASES, ids=[f"{s.key}-{e.value}" for s, e in CASES])
def test_sample_expectations(sample, engine):
    analysis = analyze_query(sample.sql, engine, PARTITIONS, schema=HINTS.mapping(), stats=HINTS.stats,
                             optimize=bool(sample.rewrites))
    assert analysis.syntax_ok, [f.message for f in analysis.findings]
    rules = {f.rule for f in analysis.findings}
    assert set(sample.expect) <= rules, f"missing findings: {set(sample.expect) - rules}; got {sorted(rules)}"
    if sample.rewrites:
        applied = {rw.rule for rw in analysis.optimization.rewrites}
        assert set(sample.rewrites) <= applied, f"missing rewrites: {set(sample.rewrites) - applied}; got {applied}"
    if sample.expect_none:
        loud = [f.rule for f in analysis.findings if f.severity.value in ("error", "warning")]
        assert loud == [], loud
    # the demo schema covers every table and column the samples use
    assert not {r for r in rules if r.startswith("unknown")}, [f.message for f in analysis.findings]
    assert analysis.logical_plan is not None and analysis.cost is not None


@pytest.fixture(scope="module")
def demo_con(tmp_path_factory):
    path = populate_duckdb(tmp_path_factory.mktemp("demo") / "demo.duckdb",
                           sizes={"orders": 5000, "order_items": 8000, "shipments": 2000, "events": 3000,
                                  "refunds": 300})
    con = duckdb.connect(str(path), read_only=True)
    yield con
    con.close()


@pytest.mark.parametrize("sample", [s for s in SAMPLES if s.demo and Engine.DUCKDB in s.engines],
                         ids=lambda s: s.key)
def test_samples_run_on_the_demo_database(sample, demo_con):
    baseline = sorted(demo_con.execute(sample.sql).fetchall(), key=str)
    analysis = analyze_query(sample.sql, Engine.DUCKDB, PARTITIONS, schema=HINTS.mapping(), stats=HINTS.stats)
    optimization = analysis.optimization
    if optimization is not None and optimization.changed:
        rewritten = sorted(demo_con.execute(optimization.optimized_sql).fetchall(), key=str)
        if all(rw.safety is Safety.SAFE for rw in optimization.rewrites):
            assert rewritten == baseline, optimization.optimized_sql
    for suggestion in analysis.suggestions:
        if suggestion.safety is Safety.SAFE:
            assert sorted(demo_con.execute(suggestion.sql).fetchall(), key=str) == baseline, suggestion.rule


@pytest.mark.parametrize("engine", list(Engine))
def test_samples_parse_in_every_engine_they_claim(engine):
    for sample in SAMPLES:
        if sample.supports(engine):
            assert parse_sql(sample.sql, ENGINES[engine].sqlglot_dialect).ok, (sample.key, engine)
