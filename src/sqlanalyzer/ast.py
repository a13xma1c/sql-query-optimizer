"""Abstract syntax trees built with SQLGlot.

SQLGlot parses SQL into a tree of :class:`sqlglot.exp.Expression` objects. This
module wraps that tree in a small, serialisable :class:`AstNode` model that the
GUI, the rule-based optimizer and tests can share:

* :func:`build_ast` converts an expression into :class:`AstNode` objects that
  carry the node kind, the argument slot it occupies in its parent, a short
  label, the SQL of the subtree and the source position (line/column/offsets).
* :func:`parse_ast` parses SQL text into one tree per statement.
* :func:`ast_to_text`, :func:`ast_to_dict` and :func:`ast_to_json` render a tree.
* :func:`node_at` locates the innermost node at a character offset.
* :func:`build_ast_graph` / :func:`render_ast_svg` draw the tree with Graphviz.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import graphviz
from sqlglot import exp

from .parsing import ParseResult, parse_sql

MAX_SQL_LABEL = 80

_OPERATOR_SYMBOLS: dict[type[exp.Expression], str] = {
    exp.EQ: "=", exp.NEQ: "<>", exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<=",
    exp.And: "AND", exp.Or: "OR", exp.Not: "NOT", exp.Add: "+", exp.Sub: "-", exp.Mul: "*",
    exp.Div: "/", exp.Mod: "%", exp.DPipe: "||", exp.Is: "IS", exp.In: "IN", exp.Like: "LIKE",
    exp.ILike: "ILIKE", exp.Between: "BETWEEN", exp.Exists: "EXISTS", exp.NullSafeEQ: "<=>",
    exp.NullSafeNEQ: "IS DISTINCT FROM", exp.BitwiseAnd: "&", exp.BitwiseOr: "|",
}

_KIND_FILL = {
    "statement": "#dbe8fb", "clause": "#e9eef5", "table": "#fde2e2", "column": "#dff3e3",
    "literal": "#fff2cc", "operator": "#ece0f7", "function": "#ffe4cc", "other": "#ffffff",
}


@dataclass(eq=False)
class AstNode:
    """One node of the abstract syntax tree."""

    id: int
    kind: str  # SQLGlot expression class name, e.g. ``Select`` or ``Column``
    arg: str  # slot in the parent (``where``, ``expressions[1]``); empty for the root
    label: str  # short human readable text (column name, literal, operator symbol, ...)
    depth: int
    line: int | None = None  # 1-based start position in the source text
    col: int | None = None
    start: int | None = None  # 0-based character offsets (inclusive start, inclusive end)
    end: int | None = None
    children: list[AstNode] = field(default_factory=list)
    expression: exp.Expression | None = field(default=None, repr=False, compare=False)
    dialect: str | None = field(default=None, repr=False, compare=False)
    _sql: str | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------ helpers
    @property
    def sql(self) -> str:
        """SQL of the subtree (truncated to MAX_SQL_LABEL characters); generated lazily."""
        if self._sql is None:
            self._sql = _truncate(self.expression.sql(dialect=self.dialect)) if self.expression is not None else ""
        return self._sql

    def walk(self) -> Iterator[AstNode]:
        yield self
        for child in self.children:
            yield from child.walk()

    @property
    def size(self) -> int:
        return sum(1 for _ in self.walk())

    @property
    def height(self) -> int:
        return 1 + max((c.height for c in self.children), default=0)

    @property
    def title(self) -> str:
        return f"{self.kind} {self.label}".strip()

    @property
    def group(self) -> str:
        """Coarse classification used for colouring."""
        e = self.expression
        if e is None:
            return "other"
        if isinstance(e, (exp.Query, exp.DML, exp.DDL, exp.Command)):
            return "statement"
        if isinstance(e, (exp.From, exp.Where, exp.Group, exp.Having, exp.Order, exp.Limit, exp.Offset,
                          exp.Join, exp.With, exp.CTE, exp.Qualify, exp.Window)):
            return "clause"
        if isinstance(e, exp.Table):
            return "table"
        if isinstance(e, (exp.Column, exp.Identifier, exp.Star, exp.Alias, exp.TableAlias)):
            return "column"
        if isinstance(e, (exp.Literal, exp.Boolean, exp.Null, exp.DataType, exp.Interval)):
            return "literal"
        if isinstance(e, exp.Func):
            return "function"
        if isinstance(e, (exp.Binary, exp.Unary, exp.Predicate, exp.Condition)):
            return "operator"
        return "other"

    def to_dict(self, include_sql: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "kind": self.kind}
        if self.arg:
            d["arg"] = self.arg
        if self.label:
            d["label"] = self.label
        if include_sql:
            d["sql"] = self.sql
        if self.line is not None:
            d["line"], d["col"] = self.line, self.col
        if self.children:
            d["children"] = [c.to_dict(include_sql) for c in self.children]
        return d


@dataclass
class AstStats:
    nodes: int
    depth: int
    leaves: int
    kinds: dict[str, int]

    @property
    def top_kinds(self) -> list[tuple[str, int]]:
        return sorted(self.kinds.items(), key=lambda kv: (-kv[1], kv[0]))[:10]


# ------------------------------------------------------------------------------ build
def node_label(e: exp.Expression) -> str:
    """Short text shown next to the node kind."""
    if isinstance(e, exp.Identifier):
        return e.name
    if isinstance(e, exp.Literal):
        return repr(e.this) if e.is_string else e.this
    if isinstance(e, exp.Boolean):
        return "TRUE" if e.this else "FALSE"
    if isinstance(e, exp.Null):
        return "NULL"
    if isinstance(e, (exp.Column, exp.Table, exp.DataType, exp.Star, exp.Var, exp.Interval)):
        return e.sql()
    if isinstance(e, exp.Alias):
        return f"AS {e.alias}" if e.alias else ""
    if isinstance(e, exp.TableAlias):
        return e.name
    if isinstance(e, exp.CTE):
        return e.alias
    if isinstance(e, exp.Join):
        return " ".join(x for x in (e.side, e.kind, "JOIN") if x)
    if isinstance(e, exp.Ordered):
        return "DESC" if e.args.get("desc") else "ASC"
    if isinstance(e, exp.SetOperation):
        return "DISTINCT" if e.args.get("distinct") else "ALL"
    if isinstance(e, exp.Select):
        return "DISTINCT" if e.args.get("distinct") else ""
    if isinstance(e, exp.Cast):
        return f"AS {e.to.sql()}" if e.args.get("to") else ""
    if isinstance(e, exp.Anonymous):
        return e.name
    for cls, symbol in _OPERATOR_SYMBOLS.items():
        if type(e) is cls:
            return symbol
    if isinstance(e, exp.Func):
        return e.sql_name()
    return ""


def _span(e: exp.Expression) -> tuple[int | None, int | None, int | None, int | None]:
    """(line, col, start, end) from tokenizer metadata of the subtree (best effort)."""
    first: dict | None = None
    end: int | None = None
    for n in e.walk():
        meta = n.meta
        if "start" not in meta:
            continue
        if first is None or meta["start"] < first["start"]:
            first = meta
        if end is None or meta["end"] > end:
            end = meta["end"]
    if first is None:
        return None, None, None, None
    col = first["col"] - (first["end"] - first["start"])
    return first["line"], max(col, 1), first["start"], end


def _truncate(sql: str) -> str:
    sql = " ".join(sql.split())
    return sql if len(sql) <= MAX_SQL_LABEL else sql[: MAX_SQL_LABEL - 1] + "…"


def build_ast(expression: exp.Expression, dialect: str | None = None) -> AstNode:
    """Convert a SQLGlot expression into an :class:`AstNode` tree (pre-order ids)."""
    counter = 0

    def build(e: exp.Expression, arg: str, depth: int) -> AstNode:
        nonlocal counter
        line, col, start, end = _span(e)
        node = AstNode(counter, type(e).__name__, arg, node_label(e), depth, line, col, start, end,
                       expression=e, dialect=dialect)
        counter += 1
        for key, value in e.args.items():
            if isinstance(value, exp.Expression):
                node.children.append(build(value, key, depth + 1))
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, exp.Expression):
                        node.children.append(build(item, f"{key}[{i}]", depth + 1))
        return node

    return build(expression, "", 0)


def parse_ast(sql: str, dialect: str) -> tuple[list[AstNode], ParseResult]:
    """Parse ``sql`` and build one tree per statement; syntax errors are in the ParseResult."""
    parsed = parse_sql(sql, dialect)
    return [build_ast(s, dialect) for s in parsed.statements], parsed


# --------------------------------------------------------------------------- queries
def ast_stats(root: AstNode) -> AstStats:
    kinds: Counter[str] = Counter()
    leaves = 0
    for n in root.walk():
        kinds[n.kind] += 1
        if not n.children:
            leaves += 1
    return AstStats(root.size, root.height, leaves, dict(kinds))


def node_at(root: AstNode, offset: int) -> AstNode | None:
    """Innermost node whose source span covers the character ``offset``."""
    best: AstNode | None = None
    for n in root.walk():
        if n.start is not None and n.end is not None and n.start <= offset <= n.end:
            if best is None or (n.end - n.start) <= (best.end - best.start):  # type: ignore[operator]
                best = n
    return best


def find_nodes(root: AstNode, kind: str) -> list[AstNode]:
    kind = kind.lower()
    return [n for n in root.walk() if n.kind.lower() == kind]


# -------------------------------------------------------------------------- rendering
def ast_to_text(root: AstNode, indent: str = "  ", with_sql: bool = False) -> str:
    lines: list[str] = []
    for n in root.walk():
        prefix = indent * n.depth
        slot = f"{n.arg}: " if n.arg else ""
        line = f"{prefix}{slot}{n.title}"
        if with_sql and n.children:
            line += f"    -- {n.sql}"
        lines.append(line)
    return "\n".join(lines)


def ast_to_dict(root: AstNode, include_sql: bool = True) -> dict[str, Any]:
    return root.to_dict(include_sql)


def ast_to_json(root: AstNode, include_sql: bool = True, indent: int = 2) -> str:
    return json.dumps(ast_to_dict(root, include_sql), indent=indent)


def _esc(text: str, limit: int = 40) -> str:
    text = text.replace("\n", " ")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_ast_graph(root: AstNode, max_nodes: int = 400, prefix: str = "a") -> graphviz.Digraph:
    """Graphviz drawing of the tree. Large trees are cut off after ``max_nodes`` nodes."""
    g = graphviz.Digraph("ast", graph_attr={"rankdir": "TB", "nodesep": "0.2", "ranksep": "0.4",
                                            "bgcolor": "transparent"},
                         node_attr={"shape": "box", "style": "rounded,filled", "fontname": "Helvetica",
                                    "fontsize": "9", "margin": "0.08,0.04", "color": "#888888"},
                         edge_attr={"fontname": "Helvetica", "fontsize": "8", "color": "#666666"})
    count = 0
    for n in root.walk():
        if count >= max_nodes:
            g.node(f"{prefix}_more", label=f"… {root.size - max_nodes} more nodes", fillcolor="#ffffff",
                   style="dashed")
            break
        label = f"<B>{_esc(n.kind)}</B>" + (f"<BR/>{_esc(n.label)}" if n.label else "")
        g.node(f"{prefix}{n.id}", label=f"<{label}>", fillcolor=_KIND_FILL[n.group], tooltip=n.sql or n.kind)
        count += 1
    drawn = {n.id for n in list(root.walk())[:max_nodes]}
    for n in root.walk():
        for c in n.children:
            if n.id in drawn and c.id in drawn:
                g.edge(f"{prefix}{n.id}", f"{prefix}{c.id}", label=c.arg)
    return g


def graphviz_available() -> bool:
    return shutil.which("dot") is not None


def render_ast_svg(root: AstNode, max_nodes: int = 400) -> bytes:
    if not graphviz_available():
        raise RuntimeError("The Graphviz 'dot' executable was not found.")
    return build_ast_graph(root, max_nodes).pipe(format="svg")
