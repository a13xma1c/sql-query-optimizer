# SQL Analyzer

A desktop app that helps developers understand SQL query performance across
**PostgreSQL, DuckDB, Trino and Snowflake**.

Stack: Python 3.11+, [SQLGlot](https://github.com/tobymao/sqlglot) (parsing, formatting, static analysis),
PySide6 (GUI), Graphviz (plan graphs), SQLAlchemy (connections / `EXPLAIN`). Managed with [uv](https://docs.astral.sh/uv/).

## Features

| Feature | How it works |
| --- | --- |
| Syntax validation | SQLGlot parses in the selected dialect; errors are underlined with line/column. |
| Formatting & linting | `Format` pretty-prints; rules cover `SELECT *`, `= NULL`, `NOT IN (subquery)`, `UNION` vs `UNION ALL`, `LIMIT` without `ORDER BY`, ... |
| Plan visualization | `EXPLAIN` output is parsed into a common model and drawn with Graphviz (zoom, pan, SVG export) next to an operator tree with row/cost/time columns. |
| Expensive joins & scans | Static: Cartesian products, non-equi joins, `OR`/functions in join keys, correlated subqueries, non-sargable predicates, repeated scans. Plan-based: nested loops without index, row explosion, large filtered-away scans, spills, bad row estimates, hot operators. |
| Complexity analysis | 0-100 score from joins, subqueries, nesting depth, CTEs, windows, set operations, correlated subqueries, ... |
| Compare versions | Analyze two queries side by side: resolved/introduced findings, complexity delta, aligned operator diff and colour-coded plan graphs. |
| Partition pruning | Static (wrapped partition columns, `OR`, leading-wildcard `LIKE`, missing/dynamic filters) using declared partition hints, plus plan-based (Trino unconstrained partition keys, Snowflake micro-partitions scanned, PostgreSQL `Append` with no pruned subplans). |
| Syntax tree | The SQLGlot AST of every statement as a navigable tree (node kind, slot in the parent, SQL of the subtree, source position), with kind filter, Graphviz drawing and JSON/text export. Double-click jumps to the source. |
| Rule-based optimizer | A rewrite engine applies ~25 rules to the AST until nothing changes and lists every rewrite with a before/after fragment, an explanation and a safety level. The rewritten SQL can replace the editor text. See below. |

## Run

```bash
brew install graphviz        # or: apt install graphviz  (the `dot` binary is required for plan graphs)
uv sync
uv run sqlanalyzer           # or: uv run python -m sqlanalyzer
```

Without a database connection you still get all static analysis, and you can paste or open a saved plan
(`EXPLAIN (FORMAT JSON)` for PostgreSQL/DuckDB, `EXPLAIN USING JSON` for Snowflake, `EXPLAIN (TYPE DISTRIBUTED)` /
`EXPLAIN ANALYZE` text for Trino).

### Connections

`Connection…` configures the selected engine. Passwords live in memory only. Other fields are remembered via
`QSettings`. `ANALYZE` *executes* the query, so it is limited to read-only statements and asks for confirmation.

### Optimizer

The *Optimize* tab shows the query after the rule engine (`sqlanalyzer.optimizer`) has rewritten it.
Every rule is either **safe** (provably the same result) or **needs review** (usually what you want, but results
can differ in edge cases - these can be switched off with one checkbox or individually via *Rules…*):

| Category | Rules |
| --- | --- |
| Predicates | `simplify` (constant folding, tautologies, absorption, `DATE_TRUNC` ranges), `sargable-date` (`DATE(ts) = d` / `YEAR(ts) = y` → ranges), `prefix-match` (`SUBSTR(x,1,n) = 'abc'` → `x LIKE 'abc%'`), `like-to-equality`, `single-value-in`, `or-to-in`, `null-comparison` (`= NULL` → `IS NULL`, review) |
| Joins | `explicit-join` (comma joins → `JOIN … ON`, Cartesian products become explicit `CROSS JOIN`), `left-join-to-inner` (when `WHERE` rejects NULLs), `duplicate-join-predicate`, `optimize-joins`, `eliminate-joins` |
| Subqueries / CTEs | `unused-cte`, `distinct-in-semijoin`, `exists-select-1`, `subquery-order-by` (review), `not-in-to-not-exists` (review), `merge-subqueries`, `dedupe-subqueries`, `pushdown-predicates`, `pushdown-projections`, `unnest-subqueries` (opt-in) |
| Aggregation / set ops | `having-to-where`, `redundant-distinct`, `union-all` (review) |
| Naming | `qualify` (resolve columns, expand `SELECT *` from the declared schema) |

The engine works on a copy of the tree, validates every result (it must re-parse in the dialect and must not
reference unknown aliases) and discards rewrites that fail, so a rule can never corrupt the query. Rules that need
column→table resolution (pushdown, merging, join elimination, star expansion) only run when the query can be
*qualified*: declare tables in the *Schema* tab, or qualify every column and avoid `SELECT *`.

```python
from sqlanalyzer.optimizer import optimize_query
result = optimize_query("SELECT * FROM a, b WHERE a.id = b.a_id AND DATE(a.ts) = '2024-01-01'", "postgres",
                        schema={"a": {"id": "int", "ts": "timestamp"}, "b": {"a_id": "int"}})
print(result.optimized_sql)
for rw in result.rewrites:
    print(rw.rule, rw.safety, rw.before, "->", rw.after)
```

Adding a rule: subclass `sqlanalyzer.optimizer.Rule`, implement `apply(expression, ctx)` (mutate the tree and
return `(root, [self.rewrite(before, after, why, ctx)])`) and register it in `optimizer/rules/__init__.py`.
Set `safety = Safety.REVIEW` when the result may differ, `requires_qualified = True` when the rule needs resolved
columns, and `fixes` to the static finding rules it addresses (the *Findings* tab then marks them as fixed).

### Schema hints

The *Schema* tab takes one table per line; types are optional and all tables must use the same qualification depth:

```
orders: id int, customer_id int, order_date date, status varchar
sales.customers: id int, name text
```

### Partition hints

The *Partition hints* tab takes one table per line, e.g.

```
analytics.events: event_date
logs: dt, region
*: ds
```

Without hints, columns named like `dt`, `event_date`, `year`, ... are treated as probable partition columns and
reported only as hints.

## Development

```bash
uv run pytest        # ~370 tests, see below
uv run ruff check .
```

Layout: `src/sqlanalyzer/` - `parsing`, `ast` (tree model), `static`, `partition`, `complexity` (SQLGlot based,
GUI-free), `optimizer/` (`engine`, `schema`, `rules/`), `plans/` (per-engine parsers, `analysis`, `diff`, `graph`),
`db` (SQLAlchemy), `analyzer` (facade), `gui/`.

Test suite (`tests/`):

| File | Covers |
| --- | --- |
| `test_static.py`, `test_partition_complexity.py` | parsing, lint/join/scan rules, partition hints, complexity score |
| `test_ast.py` | AST model: structure mirrors SQLGlot, positions, `node_at`, text/JSON rendering, Graphviz |
| `test_optimizer_rules.py` | every rule in isolation: positive, negative and edge cases, safety levels, all four dialects |
| `test_optimizer_engine.py` | pass loop and termination, validation of broken/raising/oscillating rules, review/disable switches, schema hints, analyzer integration |
| `test_optimizer_equivalence.py` | executes original and rewritten queries on DuckDB (data with NULLs/duplicates) and asserts identical results for safe rules and only the documented differences for review rules |
| `test_plans.py`, `test_duckdb_live.py` | plan parsing/analysis/diff fixtures and a live DuckDB `EXPLAIN` round trip |
| `test_gui.py` | offscreen Qt tests of every tab, including the asynchronous optimizer and the syntax tree view |

## Limitations

* Static rules work on the SQL text only; without catalog information they use heuristics (aliases, name patterns).
* The optimizer cannot know column types without a schema: `sargable-date` is then reported as *review*, and the
  schema-dependent rules are skipped (the *Optimize* tab says so). Rewrites are meant to be read, not applied blindly.
* PostgreSQL, Trino and Snowflake plan parsing is tested against fixtures, not live servers; the DuckDB path is tested end to end.
* Trino `EXPLAIN ANALYZE` text is parsed on a best-effort basis.
