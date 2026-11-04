"""Turn a :class:`Plan` into a plain-language, step-by-step explanation.

Works for plans parsed from a real ``EXPLAIN`` *and* for the logical plan
:mod:`sqlanalyzer.plans.logical` derives from the SQL text, so the user gets an
explanation of how the query will be executed without any database connection.

Steps are numbered in execution order (children before parents, left to right)
and carry notes: generic remarks about the operator plus any plan findings that
belong to the node.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..dialects import ENGINES, Engine
from ..models import Finding
from .logical import FILTER, LIMIT, MATERIALIZE, WINDOW
from .model import AGG, APPEND, EXCHANGE, INDEX_SCAN, JOIN, SCAN, SORT, Plan, PlanNode

_ALGO_TEXT = {
    "hash": ("hash join", "the smaller input is loaded into a hash table, then the other side is probed once - "
                          "usually the cheapest choice for equality joins"),
    "nested_loop": ("nested loop join", "for every row of the outer input the inner input is searched; fast only "
                                        "when the inner side is tiny or indexed on the join key"),
    "merge": ("merge join", "both inputs are read in join-key order and merged; it needs sorted inputs (an index "
                            "or an explicit sort)"),
}
_NON_SARGABLE = re.compile(r"\b(cast|date_trunc|lower|upper|substr|substring|to_char|coalesce|year|month|date)\s*\(|::",
                           re.I)


@dataclass
class ExplainStep:
    order: int  # 1-based execution order
    node_id: int
    depth: int
    title: str
    detail: str
    metrics: str = ""
    notes: list[str] = field(default_factory=list)

    def text(self, indent: str = "  ") -> str:
        head = f"{self.order}. {self.title}"
        lines = [head, f"{indent}{self.detail}"]
        if self.metrics:
            lines.append(f"{indent}{self.metrics}")
        lines += [f"{indent}! {n}" for n in self.notes]
        return "\n".join(lines)


@dataclass
class PlanExplanation:
    plan: Plan
    headline: str
    steps: list[ExplainStep]
    summary: list[str]

    def text(self) -> str:
        parts = [self.headline, ""]
        parts += [s.text() for s in self.steps]
        if self.summary:
            parts += ["", "Summary:"] + [f"- {s}" for s in self.summary]
        return "\n".join(parts)

    def step_for(self, node_id: int) -> ExplainStep | None:
        return next((s for s in self.steps if s.node_id == node_id), None)


def explain_plan(plan: Plan, findings: list[Finding] | None = None) -> PlanExplanation:
    """Describe every operator of ``plan`` in execution order."""
    notes_by_node: dict[int, list[str]] = {}
    for f in findings or []:
        if f.node_id is not None:
            message = re.sub(r"^\[[^\]]*\]\s*", "", f.message)
            notes_by_node.setdefault(f.node_id, []).append(
                message + (f" {f.suggestion}" if f.suggestion else ""))

    steps: list[ExplainStep] = []

    def walk(node: PlanNode, depth: int) -> None:
        for child in node.children:
            walk(child, depth + 1)
        steps.append(ExplainStep(len(steps) + 1, node.id, depth, _title(node), _detail(node, plan),
                                 _metrics(node, plan), _notes(node, plan) + notes_by_node.get(node.id, [])))

    walk(plan.root, 0)
    return PlanExplanation(plan, _headline(plan), steps, _summary(plan))


def explain_text(plan: Plan, findings: list[Finding] | None = None) -> str:
    return explain_plan(plan, findings).text()


# ------------------------------------------------------------------------ pieces
def _headline(plan: Plan) -> str:
    label = ENGINES[plan.engine].label
    if plan.logical:
        return (f"Expected execution for {label}, derived from the SQL itself - no database was contacted. "
                "Operator order and join strategies are what a cost-based optimizer typically picks; "
                "run Explain against a connection for real cardinalities and costs.")
    if plan.analyzed:
        return f"{label} plan with measured row counts and timings (EXPLAIN ANALYZE)."
    return f"{label} plan with the optimizer's estimates (EXPLAIN); row counts are guesses, not measurements."


def _title(node: PlanNode) -> str:
    if node.relation:
        return f"{node.op} on {node.relation}" + (f" ({node.alias})" if node.alias and node.alias != node.relation else "")
    return node.op


def _detail(node: PlanNode, plan: Plan) -> str:
    kind, op = node.kind, node.op.lower()
    if kind == SCAN:
        text = f"Reads {node.relation or 'the input'}"
        text += " row by row" if "seq" in op or "scan" == op else ""
        if node.filter:
            text += f", keeping only rows where {node.filter}"
        else:
            text += "; no filter is applied here, so every row is passed on"
        return text + "."
    if kind == INDEX_SCAN:
        cond = node.condition or node.attrs.get("Index Cond") or "the index key"
        return f"Looks up matching rows of {node.relation or 'the table'} through an index on {cond}" + \
               (f", then filters with {node.filter}" if node.filter else "") + "."
    if kind == JOIN:
        algo, why = _ALGO_TEXT.get(node.join_algo or "", ("join", ""))
        jt = (node.join_type or "INNER").upper()
        if jt == "CROSS" or not (node.condition or node.attrs.get("has_condition", True)):
            return ("Combines every row of the left input with every row of the right input (Cartesian product) - "
                    "there is no join condition.")
        how = f"Joins the two inputs on {node.condition}" if node.condition else "Joins the two inputs"
        kept = {"LEFT": " keeping all rows of the left input (unmatched ones padded with NULLs)",
                "RIGHT": " keeping all rows of the right input",
                "FULL": " keeping unmatched rows of both inputs",
                "SEMI": " keeping only left rows that have a match",
                "ANTI": " keeping only left rows without a match"}.get(jt, "")
        return f"{how} using a {algo}{kept}. Here {why}." if why else f"{how}{kept}."
    if kind == AGG:
        if "distinct" in op:
            return "Removes duplicate rows, which requires hashing or sorting the whole input."
        keys = node.attrs.get("group_keys") or node.condition
        aggs = node.attrs.get("aggregates")
        if keys and keys != "(whole input)":
            text = f"Groups the rows by {keys}"
        else:
            text = "Aggregates the whole input into a single row"
        if aggs and aggs != "(none)":
            text += f" and computes {aggs}"
        return text + "."
    if kind == SORT:
        keys = node.attrs.get("sort_keys") or node.condition or "the ORDER BY keys"
        text = f"Sorts the rows by {keys}"
        if "top" in op:
            text += ", keeping only the first rows (top-N: the engine does not sort everything)"
        else:
            text += "; sorting is blocking, no row is emitted before the last input row arrives"
        return text + "."
    if kind == APPEND:
        dup = node.attrs.get("duplicates")
        text = f"Concatenates the {len(node.children) or 'two'} inputs"
        if dup == "removed":
            text += " and then removes duplicate rows (that is the extra cost of UNION over UNION ALL)"
        elif "except" in op or "intersect" in op:
            text += " and keeps only the rows required by the set operation"
        return text + "."
    if kind == EXCHANGE:
        return ("Moves rows between workers/nodes (repartition, broadcast or gather). Network shuffles are often "
                "the dominant cost in distributed engines.")
    if kind == WINDOW:
        return (f"Computes the window function(s) {node.attrs.get('functions', '')} over {node.attrs.get('partitioning', '')}; "
                "the input is partitioned and sorted first.")
    if kind == FILTER:
        return f"Evaluates {node.filter} on every row it receives and drops the rest." if node.filter else \
            "Filters the rows it receives."
    if kind == LIMIT:
        return f"Stops after the requested number of rows ({node.attrs.get('limit', 'LIMIT')})."
    if kind == MATERIALIZE:
        name = node.attrs.get("name") or node.alias or "subquery"
        what = "CTE" if node.op.lower().startswith("cte") else "derived table"
        return (f"Evaluates the {what} '{name}'; its result feeds the operator above "
                "(engines may inline it instead of materializing).")
    if op.startswith("insert") or op.startswith("update") or op.startswith("delete") or op.startswith("create"):
        return f"Applies the statement to {node.relation or 'the target table'}."
    if op == "values":
        return "Produces rows without reading a table."
    text = "Processes the rows of its input."
    if node.filter:
        text += f" Filter: {node.filter}."
    if node.condition:
        text += f" Condition: {node.condition}."
    return text


def _metrics(node: PlanNode, plan: Plan) -> str:
    if plan.logical:
        if node.est_rows is None:
            return ""
        bits = [f"~{node.est_rows:,.0f} rows (offline estimate)"]
        if node.attrs.get("model_cost") is not None:
            bits.append(f"cost {float(node.attrs['model_cost']):,.0f} units")
        if node.attrs.get("model_note"):
            bits.append(str(node.attrs["model_note"]))
        return " · ".join(bits)
    bits = []
    if node.actual_rows is not None:
        bits.append(f"{node.actual_rows:,.0f} rows actual")
        if node.est_total_rows is not None:
            bits.append(f"{node.est_total_rows:,.0f} estimated")
    elif node.est_rows is not None:
        bits.append(f"{node.est_rows:,.0f} rows estimated")
    if node.self_time_ms is not None:
        bits.append(f"{node.self_time_ms:,.2f} ms in this operator")
    elif node.time_ms is not None:
        bits.append(f"{node.time_ms:,.2f} ms including children")
    if node.self_cost is not None:
        bits.append(f"cost {node.self_cost:,.0f}")
    return " · ".join(bits)


def _notes(node: PlanNode, plan: Plan) -> list[str]:
    out: list[str] = []
    if node.kind == SCAN:
        if not node.filter and not node.attrs.get("dynamicFilters"):
            out.append("Unfiltered scan: the whole table is read. A selective predicate (ideally on an indexed, "
                       "clustered or partition column) would cut the I/O.")
        elif node.filter and _NON_SARGABLE.search(node.filter):
            out.append("The filter wraps a column in a function/cast, so indexes and partition pruning cannot be "
                       "used; compare the bare column with a constant (range) instead.")
    if node.kind == JOIN:
        if (node.join_type or "").upper() == "CROSS" or not node.condition:
            out.append("Cartesian product: the output grows with the product of both inputs. Add a join predicate "
                       "unless one side is guaranteed to return a single row.")
        elif node.join_algo == "nested_loop":
            out.append("Without an equality predicate the engine falls back to a nested loop (O(n*m)); an equi-join "
                       "key (or an index on the inner side) enables hash/merge joins.")
        if node.attrs.get("implicit"):
            out.append("The join condition comes from the WHERE clause (comma join); writing it as JOIN ... ON makes "
                       "a missing predicate obvious.")
    if node.kind == SORT and plan.logical and not any(n.kind == LIMIT for n in plan.nodes()):
        out.append("If only the first rows are needed, add LIMIT so the engine can use a cheaper top-N sort.")
    if node.kind == LIMIT and not node.attrs.get("ordered", True):
        out.append("LIMIT without ORDER BY returns an arbitrary subset; add an ORDER BY for deterministic results.")
    if node.kind == AGG and node.attrs.get("group_keys") == "(whole input)" and plan.logical:
        out.append("The whole input is aggregated; any filter should be applied before this operator.")
    if node.kind == MATERIALIZE and plan.engine is Engine.POSTGRES and node.op.lower().startswith("cte"):
        out.append("PostgreSQL inlines simple CTEs since v12; MATERIALIZED forces a temporary result.")
    return out


def _summary(plan: Plan) -> list[str]:
    nodes = plan.nodes()
    lines: list[str] = []
    tables = sorted({n.relation for n in nodes if n.kind in (SCAN, INDEX_SCAN) and n.relation})
    if tables:
        lines.append(f"Tables read: {', '.join(tables)}.")
    joins = [n for n in nodes if n.kind == JOIN]
    if joins:
        algos = ", ".join(sorted({(n.join_algo or "join").replace("_", " ") for n in joins}))
        lines.append(f"{len(joins)} join(s) ({algos}); join order shown bottom-up.")
    blocking = [n.op for n in nodes if n.kind in (SORT, AGG) or n.op.lower().startswith("distinct")]
    if blocking:
        lines.append("Blocking operators (must consume their whole input before emitting rows): "
                     + ", ".join(sorted(set(blocking))) + ".")
    if any(n.kind == EXCHANGE for n in nodes):
        lines.append("The plan shuffles data between workers; reducing the rows before the exchange helps most.")
    if plan.logical:
        if plan.stats.get("cost_model"):
            lines.append(f"Offline cost model: {plan.stats['cost_model']} Row counts come from the declared "
                         "statistics or defaults, not from the engine.")
        else:
            lines.append("No cardinalities are available offline: operator order, not cost, is what this plan shows.")
    else:
        if plan.total_time_ms:
            lines.append(f"Measured total time: {plan.total_time_ms:,.1f} ms.")
        elif plan.total_cost:
            lines.append(f"Estimated total cost: {plan.total_cost:,.0f}.")
    return lines
