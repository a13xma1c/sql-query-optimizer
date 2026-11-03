from sqlanalyzer.complexity import analyze_complexity
from sqlanalyzer.parsing import parse_sql
from sqlanalyzer.partition import analyze_partitions, parse_hints


def prules(sql: str, hints: str = "", dialect: str = "trino") -> set[str]:
    parsed = parse_sql(sql, dialect)
    assert parsed.ok
    return {f.rule for f in analyze_partitions(parsed.statements, parse_hints(hints))}


def test_parse_hints():
    assert parse_hints("# c\nA.Events: DT, region\n\nbad line\n*: ds") == {"a.events": ["dt", "region"], "*": ["ds"]}


def test_wrapped_partition_column():
    assert "partition-column-wrapped" in prules("SELECT 1 FROM ev WHERE date(dt) = DATE '2024-01-01'")
    assert "partition-column-wrapped" in prules("SELECT 1 FROM ev WHERE cast(x AS varchar) = 'a'", "ev: x")


def test_missing_filter_with_hints_is_a_warning():
    assert "missing-partition-filter" in prules("SELECT x FROM sales.ev WHERE x = 1", "sales.ev: dt")
    assert "missing-partition-filter" in prules("SELECT x FROM ev WHERE x = 1", "ev: dt")
    assert "missing-partition-filter" not in prules("SELECT x FROM ev WHERE dt >= DATE '2024-01-01'", "ev: dt")


def test_heuristic_only_hints_at_possible_problem():
    assert "possible-missing-partition-filter" in prules("SELECT event_date, count(*) FROM ev GROUP BY 1")
    assert prules("SELECT x FROM ev WHERE dt = '2024-01-01'") == set()


def test_or_dynamic_and_like():
    assert "partition-or-filter" in prules("SELECT 1 FROM ev e WHERE e.dt = '2024-01-01' OR e.k = 3")
    assert "partition-dynamic-filter" in prules("SELECT 1 FROM ev WHERE dt = (SELECT max(dt) FROM other)", "ev: dt")
    assert "partition-like-wildcard" in prules("SELECT 1 FROM ev WHERE dt LIKE '%2024'", "ev: dt")
    assert prules("SELECT 1 FROM ev WHERE dt LIKE '2024%'", "ev: dt") == set()


def test_partition_filter_via_join_condition_counts_as_dynamic():
    r = prules("SELECT 1 FROM ev e JOIN d ON e.dt = d.dt WHERE d.k = 1", "ev: dt")
    assert "partition-dynamic-filter" in r and "missing-partition-filter" not in r


def test_complexity_scales_with_query_size():
    simple = analyze_complexity(parse_sql("SELECT a FROM t WHERE a = 1", "postgres").statements)
    big_sql = (
        "WITH a AS (SELECT * FROM x JOIN y ON x.i = y.i JOIN z ON z.i = y.i), "
        "b AS (SELECT k, row_number() OVER (PARTITION BY k ORDER BY v) rn, sum(v) s FROM a GROUP BY k, v) "
        "SELECT CASE WHEN rn > 1 THEN 1 ELSE 0 END, (SELECT max(q) FROM w WHERE w.k = b.k) "
        "FROM b JOIN c ON b.k = c.k WHERE b.k IN (SELECT k FROM m WHERE m.z IN (SELECT z FROM deep)) AND EXISTS (SELECT 1 FROM n WHERE n.k = b.k) "
        "UNION ALL SELECT 1, 2 FROM dual"
    )
    big = analyze_complexity(parse_sql(big_sql, "postgres").statements)
    assert simple.level == "Low" and simple.score < 10
    assert big.score > simple.score + 30
    assert big.metrics["ctes"] == 2 and big.metrics["window_functions"] == 1
    assert big.metrics["max_nesting_depth"] >= 3 and big.metrics["correlated_subqueries"] >= 2
    assert simple.findings() == []
