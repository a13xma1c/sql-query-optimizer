# SQL Analyzer

A desktop app that helps developers understand SQL query performance across
**PostgreSQL, DuckDB, Trino and Snowflake**.

Stack: Python 3.12+, [SQLGlot](https://github.com/tobymao/sqlglot) (parsing, formatting, static analysis),
PySide6 (GUI), Graphviz (plan graphs), SQLAlchemy (connections / `EXPLAIN`). Managed with [uv](https://docs.astral.sh/uv/).

## Features

| Feature | How it works |
| --- | --- |
| Syntax validation | SQLGlot parses in the selected dialect; errors are underlined with line/column. |
| Schema-aware analysis | With a declared or imported schema: unknown tables/columns/aliases, ambiguous unqualified columns, **join keys of different types** (`VARCHAR = INTEGER`), literals of the wrong type, joins on non-key columns of large tables, how many columns `SELECT *` really reads (`schema` category). |
| Catalog introspection | *Schema → Import from database…* reads tables, columns, types, primary/unique keys, indexes, **real statistics** (row counts, distinct values) and partition/clustering keys from DuckDB, PostgreSQL, Trino or Snowflake - scoped to the tables of the current query if you like - into the same hints text the Schema tab uses. |
| Formatting & linting | `Format` pretty-prints; rules cover `SELECT *`, `= NULL`, `NOT IN (subquery)`, `UNION` vs `UNION ALL`, `LIMIT` without `ORDER BY`, ... |
| Plan visualization | `EXPLAIN` output is parsed into a common model and drawn with Graphviz (zoom, pan, click-to-select, collapsible subtrees, heat-map, SVG/PNG/PDF export) next to an operator tree with row/cost/time columns. |
| Plan explanation (offline) | Without any connection the plan is derived from the SQLGlot AST (scan → filter → join → aggregate → window → sort → limit) and explained operator by operator in plain language, with notes on full scans, Cartesian products, non-sargable filters, blocking operators … When a real `EXPLAIN` is loaded the same view explains it, enriched with measured rows/times and the plan findings. |
| Expensive joins & scans | Static: Cartesian products, non-equi joins, `OR`/functions in join keys, correlated subqueries, non-sargable predicates, repeated scans. Plan-based: nested loops without index, row explosion, large filtered-away scans, spills, bad row estimates, hot operators. |
| Complexity analysis | 0-100 score from joins, subqueries, nesting depth, CTEs, windows, set operations, correlated subqueries, ... |
| Compare versions | Analyze two queries side by side: resolved/introduced findings, complexity delta, aligned operator diff and colour-coded plan graphs - with the offline plans when no `EXPLAIN` was run. |
| Partition pruning | Static (wrapped partition columns, `OR`, leading-wildcard `LIKE`, missing/dynamic filters) using declared partition hints, plus plan-based (Trino unconstrained partition keys, Snowflake micro-partitions scanned, PostgreSQL `Append` with no pruned subplans). |
| Syntax tree | The SQLGlot AST of every statement as a navigable tree (node kind, slot in the parent, SQL of the subtree, source position), with kind filter, Graphviz drawing and JSON/text export. Double-click jumps to the source. |
| Rule-based optimizer | A rewrite engine applies ~25 rules to the AST until nothing changes and lists every rewrite with a before/after fragment, an explanation and a safety level. The rewritten SQL can replace the editor text. See below. |
| Rewrite suggestions | Every rule is also offered on its own: the *Findings* tab shows the suggested rewrite for a finding with an *Apply suggested rewrite* button, and *Optimize → Pick rewrites* lets you tick any subset and preview the query with exactly those applied. |
| Cost model (offline) | Row counts, selectivities, join cardinalities and operator costs are estimated from the SQL and the declared statistics, per engine family (row store / columnar / distributed). The *Tuning* tab shows the estimate, the dominant operators, the assumptions made, and the cost delta of the rewrite and of the recommended indexes; the plan tree and the explanation carry the estimated rows. |
| Index recommendations | The *Tuning* tab derives physical-design advice from how each table is accessed: PostgreSQL B-tree/expression/trigram indexes, DuckDB ART indexes and sort order, Trino partitioning and `sorted_by`, Snowflake clustering keys and search optimization - with DDL, priority and the findings each one addresses. |
| Engine-specific rules | Findings in the `engine` category encode what each engine does differently (Trino/Snowflake: exact `COUNT(DISTINCT)`, global sorts, build side, VARIANT filters, columnar `SELECT *`; PostgreSQL: materialised CTEs, `DISTINCT ON`, trigram-able LIKE; all: large `OFFSET`, long `IN` lists, unanchored regex …), plus engine-restricted rewrites (`approx-count-distinct`, `ilike`, `in-list-to-any`). |
| Query history | Explicit actions (Analyze, Explain, applying a rewrite) record the query with engine, findings, complexity, estimated cost and plan source in a searchable *History* tab: reload, tag, send to Compare, export as a SQL script. |
| Local LLM rewrites (optional) | A model running on your machine (Ollama or any OpenAI-compatible server) is asked for a rewrite; the answer is only applicable after it passes the validation against the rule set (syntax, sources, projection, predicates, query shape, static findings, rule engine). See below. |
| Plan graph interaction | Clicking a node in the Graphviz plan graph selects it in the operator tree (and vice versa via the step links in the offline explanation); double-clicking a node with children collapses/expands its subtree, so large plans stay navigable. A **heat-map** mode colours operators by their share of total cost/time (a percentage is also printed in the label, so the colour is never the only signal). Export to **SVG, PNG or PDF**. |
| Editor autocomplete | Typing offers SQL keywords plus the tables/columns from the declared or imported schema; `alias.` (or `table.`) narrows the list to that table's columns via a tolerant scan of the `FROM`/`JOIN` clauses (works even while the rest of the query is incomplete). |
| Multiple query tabs | The editor is a tab bar (`Ctrl+T` new, `Ctrl+W` close, double-click to rename); each tab keeps its own pasted/fetched plan and file association, and titles itself from the first line of the query until one is attached to a file. |
| Diagnostics tooltips | Hovering an underlined span in the editor shows that finding's rule, message and suggestion as a tooltip - severity is shown by underline *shape* (wave/dash/dot) as well as colour, so it still reads in grayscale. |
| Light / dark theme | A toolbar menu switches the whole application (Fusion style + palette) and the syntax highlighter's colours between light, dark and "match the system"; the choice is remembered. |
| Configurable finding rules | *Findings → Rules…* enables/disables individual checks (lint, join, scan, partition, engine, schema, plan-level; ~64 in total), filterable by category or name - independent of the optimizer's own rewrite-rule toggles. |
| Saved connection profiles | *Connection…* can save named profiles (host/port/database/schema/user, and optionally the password) per engine; passwords go through the OS keyring when one is available and degrade to "not saved, ask again" otherwise - they are never written to the settings file. |

## Run

```bash
brew install graphviz        # or: apt install graphviz  (the `dot` binary is required for plan graphs)
uv sync
uv run sqlanalyzer           # or: uv run python -m sqlanalyzer
uv run sqlanalyzer --version / --self-check   # what CI runs against the wheel and the desktop bundles
```

`uv` **0.13 or newer** is needed to build the package: `pyproject.toml` pins the `uv_build` 0.13 backend, and
older `uv` front-ends (0.9.x was tried) cannot read the sdists it writes (`numeric field was not a number ...
when getting cksum`) - `uv self update` fixes that. `keyring`, `mypy`, `pyyaml` and `pyinstaller` were added to
`pyproject.toml` after `uv.lock` was last regenerated in this (offline) environment; run `uv lock` once after
pulling. The app works fine without a keyring backend too - it just will not remember profile passwords; see
*Connections* below.

Prebuilt desktop bundles (PyInstaller; Linux tarball, macOS `.app`, Windows folder) are produced by the
*Release* workflow for every `vX.Y.Z` tag - see *Packaging* under *Development*. Graphviz remains a
separate install for those as well.

**Demo environment.** *Connection… → Use demo database* builds a DuckDB file with a small e-commerce schema
(customers, products, orders, order_items, refunds, shipments, events; ~900k rows); `scripts/populate_demo.py`
(or `uv run sqlanalyzer-demo`) does the same from the command line and can also fill a PostgreSQL database
(partitioned `events`, indexes, `ANALYZE`) or print DDL + sample rows for Trino/Snowflake
(`scripts/demo_trino.sql`, `scripts/demo_snowflake.sql`, `scripts/demo_schema_hints.txt`). DuckDB files are written
in the **v1.0.0 storage format** (DuckDB 2.x would otherwise use its latest format, header version 999, which older
DuckDB builds, the CLI, JDBC drivers and other tools cannot open); this applies to the demo database, to any file
the app creates through *New…*, and can be changed with `--storage-version`. The connection dialog shows the
storage format of an existing file. The *Samples* toolbar
menu opens one of 30 bundled queries, each illustrating one pattern (the expected findings are in the file header
and verified by the tests).

Without a database connection you still get all static analysis, the rule-based optimizer, the syntax tree and an
offline plan explanation (*Explain offline*, `Ctrl+Shift+E`); you can also paste or open a saved plan
(`EXPLAIN (FORMAT JSON)` for PostgreSQL/DuckDB, `EXPLAIN USING JSON` for Snowflake, `EXPLAIN (TYPE DISTRIBUTED)` /
`EXPLAIN ANALYZE` text for Trino).

**Python 3.12 or newer is required.** PySide6 6.12 ships `abi3` wheels whose generated code returns *borrowed*
references to `None`/`True` from void methods and `Signal.emit()`. Python 3.12+ makes those singletons immortal,
so this is harmless; on 3.11 the reference counts really drop and the GUI aborts with
`Fatal Python error: none_dealloc` after a few thousand widget calls (a `QLabel.setText` loop reproduces it in
seconds). `sqlanalyzer.check_runtime()` refuses to start on older interpreters for that reason.

The main window opens sized to the available screen area (never larger than the display) and remembers its
geometry, clamped to the screens that are present at the next start. Settings (connections without passwords,
hints, optimizer and LLM preferences, window geometry) live in a JSON file in the
platform's per-user config directory (e.g. `~/.config/SQLAnalyzer/settings.json`); set `SQLANALYZER_SETTINGS_FILE`
to use another location.

### Connections

`Connection…` configures the selected engine. A connection typed in directly (not saved as a profile) keeps its
password in memory only, for this session; other fields are remembered in the JSON settings file (see below).
`ANALYZE` *executes* the query, so it is limited to read-only statements and asks for confirmation. The local-LLM
endpoint is configured separately (*Optimize → Local LLM → Settings…*) and is never contacted unless you ask for a
suggestion.

**Connection profiles.** For PostgreSQL, Trino and Snowflake, the dialog can also save named profiles: pick
*Save as profile…* after filling in the fields, and *Profile* to load one back. The non-secret fields are stored
in the settings file like everything else; the password goes through the
[`keyring`](https://pypi.org/project/keyring/) package into the OS credential store (macOS Keychain, Windows
Credential Locker, a Secret Service/KWallet on Linux) *if* a usable backend is present. When none is available
(headless Linux without a secret service is common, and this is exactly what this sandbox has) saving a profile
still works, just without the password - the dialog says so, and you type it again next time. Deleting a profile
also deletes its keyring entry, if any. This all lives in `sqlanalyzer.gui.profiles` (`keyring_available()`,
`ProfileStore`), independent of Qt, and is tested with a fake in-memory backend for the "available" path (a real
`keyring.backend.KeyringBackend` subclass is *not* used for that fake, since merely defining one leaks into
keyring's global, process-wide auto-detection for the rest of the test session).

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

### Local LLM (optional, validated)

The *Optimize → Local LLM* page sends the query, the declared schema, the partition hints, the static findings and
the rule engine's rewrite to a model on your machine and shows what it proposes. Nothing is sent until you press
*Ask local LLM*, and nothing leaves the host you configure:

* **Ollama** - `http://localhost:11434`, e.g. `ollama pull qwen2.5-coder:7b`
* **OpenAI-compatible** server (llama.cpp, LM Studio, vLLM, LocalAI) - `http://localhost:8080`

Because a model can hallucinate a table or silently drop a filter, **every answer is validated against the same
machinery the rest of the app uses** before it can be applied:

| Check | Fails when |
| --- | --- |
| `syntax` | the rewrite does not parse in the dialect |
| `statements` | the number or the type of statements changed |
| `sources` | a table appears that is neither in the query nor in the declared schema (warn: a table disappeared) |
| `projection` | columns were added/removed, or `SELECT *` was expanded without a schema to verify it against |
| `predicates` | filter/join predicates disappeared without replacement (warn: they were rewritten) |
| `query shape` | aggregates, GROUP BY keys, `DISTINCT`, `LIMIT` or `ORDER BY` were dropped - unless the rule engine drops them too |
| `static analysis` | the rewrite introduces an error-level finding (e.g. a Cartesian product) |
| `rule engine` | never fails; it reports whether the answer matches the deterministic rewrite, and which safe rules still apply afterwards |
| `changed anything` / `declared-risk` | warn when the model returned the query unchanged, or flagged a change as result-changing |

A single failure makes the suggestion non-applicable (the *Replace editor SQL* button stays disabled) while the
checks stay visible, so you see *why* it was rejected. Same API from Python:

```python
from sqlanalyzer.llm import LlmClient, LlmConfig, llm_optimize
result = llm_optimize(sql, "postgres", LlmClient(LlmConfig(model="qwen2.5-coder:7b")), schema=schema)
print(result.summary())          # "… Needs review: 9/10 checks passed, 1 warning(s)."
for check in result.review.checks:
    print(check.status, check.name, check.detail)
if result.ok:
    print(result.sql)
```

### Schema hints and statistics

The *Schema* tab takes one table per line; types and statistics are optional and all tables must use the same
qualification depth:

```
orders (rows=2000000): id int pk, customer_id int indexed, ts timestamp, status varchar ndv=4
sales.customers (rows=50000): id int unique, name text
```

`(rows=N)` is the table size, `pk`/`unique` mark keys, `indexed` (or `sorted`, `clustered`) says the engine can
seek or prune on the column, `partitioned` marks a partition/clustering key (it also feeds the partition-pruning
rules, so the *Partition hints* tab is only needed for tables that are not declared here), `ndv=N` is the number
of distinct values. They feed the schema-aware rules, the offline cost model, the index advisor (which never
recommends what is already a key or indexed) and engine rules such as Trino's build-side check.

*Import from database…* fills this text from the live catalog (`sqlanalyzer.catalog`): DuckDB
(`duckdb_tables/columns/constraints/indexes`, optional `approx_count_distinct`), PostgreSQL (`pg_class.reltuples`,
`pg_index`, `pg_stats.n_distinct`, `pg_partitioned_table`), Trino (`information_schema`, `SHOW STATS`, partition
keys from `SHOW COLUMNS` / `SHOW CREATE TABLE`) and Snowflake (`information_schema.tables` row counts and
clustering keys, `SHOW PRIMARY/UNIQUE KEYS`). Imports can be scoped to the tables of the current query and merged
with what you typed.

### Cost model, index advisor, history

The cost model (`plans/costing.py`) annotates the offline plan bottom-up with textbook heuristics: default
selectivities per predicate shape (equality 0.1 or `1/ndv`, ranges 1/3, both bounds of one interval 0.1, prefix
`LIKE` 0.1, `IS NULL` 0.05, AND/OR composition), join cardinalities from keys (a join on a unique key keeps the
other side's rows, otherwise the larger input), and engine-family costs (row stores read whole rows, columnar
engines only the referenced columns, distributed engines pay for shuffles and global sorts). A scan takes the
cheapest access path it can justify: index seek (declared keys / PostgreSQL indexed columns), zone-map or
partition pruning (indexed columns; time-named columns on columnar engines, recorded as an assumption), or a full
scan; function-wrapped columns get neither. Units are relative, every assumption is listed, and
`QueryAnalysis.cost_change` / `cost_change_with_indexes` compare the original with the rewrite and with the
recommended indexes.

The index advisor (`indexes.py`) collects, per table and query block, equality/range predicates, join keys,
ORDER BY/GROUP BY columns, function-wrapped columns and case-insensitive/wildcard LIKEs, and emits the engine's own
vocabulary (see the feature table). The history (`history.py`) is a JSON file next to the settings
(`SQLANALYZER_HISTORY_FILE` overrides it), de-duplicated by normalised SQL and engine, capped at 500 entries.

### Plan graph: click-to-select, collapse, heat-map, export

`plans/graph.py` gives every Graphviz node/edge an explicit `id=` (`n<node id>`, `e<child id>_<parent id>`),
so the rendered SVG's `<g id="...">` elements can be matched back to plan node ids. `gui/svg_view.py` resolves a
click to the smallest element under the pointer (`QSvgRenderer.boundsOnElement`/`transformForElement`, mapped
through the `QGraphicsSvgItem`; but the hit *rectangles* themselves are parsed from the SVG source's node shapes, `element_rects`, because `boundsOnElement` includes the label text and therefore varies with the platform's fonts - on Windows it reported boxes twice as wide as drawn) and tells press-from-pan apart by how far the mouse moved between press and
release, so panning (`ScrollHandDrag`) still works. The Plan tab wires this to the operator tree in both
directions, and double-click toggles `PlanTab._collapsed`, which `build_graph(..., collapsed=...)` renders as a
single dashed placeholder box instead of descending into the subtree. `heat_map(plan)` turns self-time (or, for
an unmeasured/offline plan, self-cost) into a 0..1 share per node for the heat-map fill
(`heat_color`, a 4-stop yellow→red ramp); `render_svg`/`render_png`/`render_pdf` all accept the same
`severities`/`status`/`heat`/`collapsed` arguments. Severity and diff status are additionally encoded as a glyph
in the node label (⛔/⚠ for error/warning, +/−/~ for added/removed/changed), not just the border colour.

### Editor: autocomplete, multiple tabs, diagnostics tooltips

`gui/completer.py` is plain Python (no Qt) that decides what a `QCompleter` should offer: SQL keywords plus every
declared table/column, narrowed to one table's columns after `alias.`/`table.` using a regex scan of `FROM`/`JOIN`
clauses that tolerates invalid/incomplete SQL (`table_aliases`, `context`, `candidates`). `CodeEditor.set_completer`
(in `gui/editor.py`) wires the popup using the standard Qt recipe (forward Enter/Tab/Escape while the popup is
visible, filter-as-you-type via `Ctrl+Space` or any word character); `gui/query_tabs.py::QueryTabs` attaches one to
every tab automatically. Diagnostics tooltips reuse the same underline spans `mark_findings` already computes:
hovering one shows the rule name, message and suggestion (`CodeEditor.event`, `QEvent.Type.ToolTip`). Multiple
query tabs (`QueryTabs`) give each its own editor, pasted/fetched plan text and file path; `MainWindow.editor`/
`.plan_text` are now properties that resolve to whichever tab is active, so the rest of the application did not
need to change.

### Light/dark theme and configurable finding rules

The *Theme* toolbar menu (`gui/theme.py`) switches the whole app between light, dark and "match the system" by
swapping the `Fusion` style's `QPalette` and re-deriving the SQL syntax highlighter's token colours
(`SqlHighlighter.set_palette`); `MainWindow.apply_theme` re-themes every open `CodeEditor` it can find
(`findChildren`) and remembers the choice for tabs opened afterwards.

*Findings → Rules…* (`gui/rules_dialog.py::FindingRulesDialog`) enables/disables individual *finding* rules -
distinct from the optimizer's own rewrite-rule toggles (*Optimize → Rules…*). `rules_registry.py` is the flat
catalog those two screens are built from: every rule name used by `static.py`, `static_engine.py`,
`static_schema.py`, `partition.py`, `complexity.py` and `plans/analysis.py`, with its category and a one-line
description (engine-specific rules also carry which engines they apply to, reusing `static_engine.ENGINE_RULES`'s
own metadata). Disabling a rule does not change *how* it fires - `filter_findings` just drops matching findings
after the fact, wired into `analyze_query`/`run_optimization` via `disabled_finding_rules`.

### Accessibility: not just colour

Severity and diff status are never carried by colour alone: editor underlines vary by *style* (wave/dash/dot for
error/warning/info, `editor.py::_SEVERITY_STYLE`) in addition to colour; the plan graph prefixes node labels with
a glyph (⛔/⚠/+/−/~, `plans/graph.py::_SEVERITY_GLYPH`/`_STATUS_GLYPH`) in addition to the border colour; the
heat-map prints the percentage in the label rather than relying on the gradient alone; and the Findings table's
severity column is text (`ERROR`/`WARNING`/`INFO`), not a colour swatch.

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
uv run pytest                 # ~900 tests (unit + end-to-end), see below
uv run pytest -m "not e2e"    # the fast unit/component suite only
uv run pytest tests/e2e       # end-to-end only
uv run ruff check .           # lint + import order (formatting is not enforced)
uv run mypy                   # type check: src/ strictly, tests/ for imports and annotated helpers
```

**Type checking.** `mypy` runs over `src/sqlanalyzer` with `check_untyped_defs`, `warn_unreachable`,
`strict_equality` and unused-ignore warnings, and over `tests/` with defaults (test bodies are untyped, and their
`assert optional_thing.attr` idioms are noise for a checker). Two things are worth knowing when adding code:
SQLGlot 30 split its node base class into an `Expr` trait (`exp.Expr`) with `Expression` as one subclass and
`Condition`/`Binary` as siblings, so "any node" is annotated `exp.Expr` here - `exp.Expression` is too narrow
for what `find_all`/`parse`/`.parent` return (runtime behaviour is identical, every node still is an
`Expression` through the trait MRO); and PySide6's stubs make most Qt getters `Optional`, so `popup()`,
`item(row, col)`, `QApplication.instance()` etc. get narrowed rather than ignored. The only package without
type information is `graphviz`. Two real bugs surfaced from enabling this: an attribute initialised as `None`
without an annotation (so mypy proved every later branch unreachable) and a defensive `None` check on a
call that cannot return `None`.

**Continuous integration** (`.github/workflows/ci.yml`): `ruff`, `mypy`, the test suite on Linux (3.12, 3.13),
macOS and Windows (with Graphviz and PostgreSQL's `initdb`/`pg_ctl` where the runner provides them, so the
e2e tests run for real), `uv build` + `twine check` + installing the wheel into a clean environment, and the
PyInstaller bundle on all three platforms, each verified with `sqlanalyzer --self-check`. A separate
*lockfile* job reports a stale `uv.lock` without masking the test results.

**Packaging.** `uv build` produces the wheel (package + the bundled `samples/*.sql`) and the sdist (which also
carries `tests/` and `scripts/`, per `[tool.uv.build-backend]`). `packaging/sqlanalyzer.spec` builds a
one-folder PyInstaller bundle (`uv run --group packaging pyinstaller packaging/sqlanalyzer.spec --noconfirm`;
`dist/sqlanalyzer/`, `SQL Analyzer.app` on macOS): it names every family of *dynamic* imports PyInstaller
cannot see - SQLAlchemy dialect plugins and their entry-point metadata, SQLGlot dialects, keyring backends -
and `sqlanalyzer --self-check` exercises all of them plus an off-screen construction of the main window, so
a missing module fails the build instead of a user's first click. `.github/workflows/release.yml` builds the
wheel, the sdist and the three bundles for a `vX.Y.Z` tag (after checking the tag against `pyproject.toml`
and `sqlanalyzer.__version__`), attaches them to a GitHub release, and can publish to PyPI on manual
dispatch via trusted publishing.

Layout: `src/sqlanalyzer/` - `parsing`, `ast` (tree model), `static`, `static_engine`, `static_schema`,
`rules_registry` (flat catalog of every finding rule), `partition`, `complexity` (SQLGlot based, GUI-free),
`optimizer/` (`engine`, `schema`, `rules/`), `llm/` (`client`, `prompt`, `review`, `optimize`), `plans/`
(per-engine parsers, `logical` (offline plan), `explain`, `analysis`, `diff`, `graph`, `costing`), `indexes`,
`catalog`, `demo`, `samples`, `history`, `db` (SQLAlchemy), `analyzer` (facade), `gui/` (`editor`, `completer`,
`theme`, `query_tabs`, `svg_view`, `profiles`, `rules_dialog`, `geometry`, `settings`, per-tab widgets).

Test suite (`tests/`):

| File | Covers |
| --- | --- |
| `test_static.py`, `test_partition_complexity.py` | parsing, lint/join/scan rules, partition hints, complexity score |
| `test_ast.py` | AST model: structure mirrors SQLGlot, positions, `node_at`, text/JSON rendering, Graphviz |
| `test_optimizer_rules.py` | every rule in isolation: positive, negative and edge cases, safety levels, all four dialects |
| `test_optimizer_engine.py` | pass loop and termination, validation of broken/raising/oscillating rules, review/disable switches, schema hints, analyzer integration |
| `test_optimizer_equivalence.py` | executes original and rewritten queries on DuckDB (data with NULLs/duplicates) and asserts identical results for safe rules and only the documented differences for review rules |
| `test_plans.py`, `test_duckdb_live.py` | plan parsing/analysis/diff fixtures, Graphviz node/edge ids, heat-map, collapsed subtrees, glyphs, PNG/PDF export, and a live DuckDB `EXPLAIN` round trip |
| `test_plan_offline.py` | the connection-free logical plan (shapes, join strategies, CTEs, DML, every dialect) and the explanation of both offline and real plans |
| `test_llm_client.py` | the local-LLM transport (both providers, a real HTTP round trip against a stub server), prompt content and answer parsing |
| `test_llm_review.py` | the validation matrix: hallucinated tables, dropped predicates/columns/LIMIT, introduced findings, rule-engine agreement |
| `test_llm_optimize.py` | end-to-end with a stubbed model, including a DuckDB check that an accepted rewrite returns the same rows |
| `test_costing.py` | the offline cost model: selectivities, access paths, join cardinalities, engine families, assumptions, cost deltas |
| `test_indexes.py` | index / physical-design recommendations for each engine, priorities, de-duplication, declared keys |
| `test_engine_rules.py` | engine-specific findings and the engine-restricted rewrite rules (incl. a DuckDB round trip) |
| `test_suggestions.py`, `test_history.py` | per-rule suggestions and selective application (DuckDB-verified), the history store |
| `test_static_schema.py` | schema-aware rules: type families, join-key/literal type checks, unknown/ambiguous names, wide stars |
| `test_catalog.py` | introspection of a live DuckDB (incl. the demo database) and canned PostgreSQL/Trino/Snowflake catalogs, round trip through the hints text |
| `test_samples.py`, `test_demo.py` | every bundled sample on every engine it lists (expected findings/rewrites from the headers, execution on the demo database), the demo population/DDL/CLI |
| `test_geometry.py` | window-geometry maths, fitting/clamping to the screen(s), settings round trip, DuckDB storage-version detection |
| `test_rules_registry.py` | the finding-rule catalog (uniqueness, categories, engine restrictions) and `filter_findings`/`disabled_finding_rules` wired into `analyze_query` |
| `test_theme.py` | theme resolution (`system` -> light/dark from the current palette), the dark `QPalette`, `apply_theme` idempotence |
| `test_completer.py` | autocomplete candidate logic: alias/table detection tolerant of invalid SQL, qualifier/prefix parsing, column lookup across qualification depths |
| `test_editor.py` | the code editor: per-severity underline style+colour, diagnostics tooltips, autocomplete popup wiring (`Ctrl+Space`, completion insertion, replacing a completer) |
| `test_packaging.py` | version consistency (`__version__` vs `pyproject.toml`), console scripts, package data, `--version`/`--self-check` (in-process and as a subprocess), the PyInstaller spec's hidden-import coverage, workflow YAML |
| `test_profiles.py` | connection profiles and keyring-backed passwords: the "no backend" default of this sandbox, a fake in-memory backend for the "available" path, persistence, per-engine scoping, id de-duplication, deletion |
| `test_gui.py` | offscreen Qt tests of every tab, including the asynchronous optimizer, the syntax tree, the offline plan explanation, the LLM panel, the Tuning and History tabs, per-finding fixes, the rewrite picker, the DuckDB connection dialog, connection profiles, the schema import, the samples menu, plan-graph click/collapse/heat-map/export, query tabs, autocomplete, theming and the finding-rules dialog |
| `e2e/test_engines_e2e.py` | the complete pipeline per engine (connect → `EXPLAIN` → parse → analyze → explain → rule rewrite → re-`EXPLAIN` → compare → validated LLM rewrite) |
| `e2e/test_postgres_e2e.py` | PostgreSQL stories on a real server: `EXPLAIN (ANALYZE, BUFFERS)`, a rewrite turning a `Seq Scan` into an index scan, partition pruning enabled by `simplify`, offline vs. real plan shape |
| `e2e/test_gui_e2e.py` | user journeys through the Qt UI with real database round trips: explain/analyze with confirmation, compare versions with engine plans, connection/paste/file dialogs, keyboard actions, LLM journey, settings persistence |

**End-to-end tests per engine.** The `e2e` suite runs the *same* scenarios against every engine; what stands
behind the connection depends on what is available:

| Engine | How it runs |
| --- | --- |
| DuckDB | always live, in-process, on a temporary file with 200k sample orders |
| PostgreSQL | live against `SQLANALYZER_E2E_POSTGRES_URL` if set; otherwise a throw-away cluster is started with the local `initdb`/`pg_ctl` (skipped when they are missing or refuse to run - `initdb` will not start as root, nor with Administrator privileges on Windows, which is how GitHub's Windows runners execute) |
| Trino | live against `SQLANALYZER_E2E_TRINO_URL` (`trino://user@host:8080/catalog/schema`, tables `customers`, `orders`, `refunds` must exist); otherwise a **recorded server**: the app still builds the SQLAlchemy URL, issues `EXPLAIN (TYPE DISTRIBUTED)` and parses rows in the driver's shape, but the rows come from `tests/data/trino_plan.txt` |
| Snowflake | same with `SQLANALYZER_E2E_SNOWFLAKE_URL` (`snowflake://user:pw@account/DB/SCHEMA?warehouse=WH`) and `tests/data/snowflake_plan.json` |

Live servers get the sample schema created (and dropped) by the fixtures; the Trino/Snowflake URLs are expected
to point at a schema that already contains the sample tables.

**Clean shutdown.** DuckDB's nanobind bindings print `nanobind: leaked N instances` when Python tears down the
`duckdb` modules before the objects they hold (module-level type constants) are released - the order depends on
the import order, which worker threads make non-deterministic, and widgets that are only destroyed at interpreter
exit make it worse. The app registers `sqlanalyzer.db.release_duckdb` with `atexit` and destroys its window
explicitly (`sqlanalyzer.shutdown`); the test suite does the same and additionally processes pytest-qt's deferred
widget deletions after every test (`tests/conftest.py`), so `pytest` ends without that report
(`tests/test_shutdown.py` checks it in subprocesses).


## Limitations

* Static rules work on the SQL text only; without catalog information they use heuristics (aliases, name patterns).
* The optimizer cannot know column types without a schema: `sargable-date` is then reported as *review*, and the
  schema-dependent rules are skipped (the *Optimize* tab says so). Rewrites are meant to be read, not applied blindly.
* The offline plan shows the operator *order and strategy* an optimizer would typically choose, never cardinalities
  or costs - those require a real `EXPLAIN`. Plan-level findings are therefore only produced for engine plans.
* LLM suggestions are validated, not proven: the checks catch dropped predicates, columns, tables and shapes, but
  equivalence of arbitrary expressions is not decidable here. Treat a "validated" rewrite as a reviewed proposal.
* PostgreSQL, Trino and Snowflake plan parsing is tested against fixtures, not live servers; the DuckDB path is tested end to end.
* Trino `EXPLAIN ANALYZE` text is parsed on a best-effort basis.
