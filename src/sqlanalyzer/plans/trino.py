"""Trino text plan parser (``EXPLAIN`` / ``EXPLAIN (TYPE DISTRIBUTED)`` / ``EXPLAIN ANALYZE``)."""

from __future__ import annotations

import re

from ..dialects import Engine
from .model import AGG, EXCHANGE, JOIN, OTHER, SCAN, SORT, IdCounter, Plan, PlanNode, PlanParseError, to_float

_FRAGMENT = re.compile(r"^Fragment\s+(\d+)\s*\[(.*?)\]")
_ROOT = re.compile(r"^ {4}(\w+)(\[.*\])?\s*$")
_CONNECTOR = re.compile(r"^([ │]*)([├└])─ (.*)$")
_HEADER_PREFIXES = ("Output layout", "Output partitioning", "Input partitioning", "CPU:", "Amount of input",
                    "Partitioning", "Per-node")
_MULT = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "G": 1e9, "T": 1e12}
_TIME = {"ns": 1e-6, "us": 1e-3, "μs": 1e-3, "ms": 1.0, "s": 1e3, "m": 6e4, "h": 3.6e6, "d": 8.64e7}


def _num(text: str) -> float | None:
    m = re.fullmatch(r"([\d.,]+)\s*([KMBGT]?)", text.strip())
    if not m:
        return None
    value = to_float(m.group(1))
    return value * _MULT[m.group(2)] if value is not None else None


def _kind(op: str) -> str:
    low = op.lower()
    if low in ("crossjoin",) or low.endswith("join") or low in ("semijoin", "spatialjoin"):
        return JOIN
    if "scan" in low:
        return SCAN
    if low.startswith("aggregate") or low == "aggregation":
        return AGG
    if low in ("sort", "topn", "topnranking", "mergeexchange"):
        return SORT
    if low in ("remotesource", "localexchange", "exchange", "remoteexchange", "mergesource"):
        return EXCHANGE
    return OTHER


def _is_trino(text: str) -> bool:
    return bool(re.search(r"^Fragment\s+\d+\s*\[", text, re.M)) or bool(
        re.search(r"^\s*(└─|├─|Output\[|\s+Layout:)", text, re.M))


def parse_trino(text: str) -> Plan:
    if not isinstance(text, str) or not _is_trino(text):
        raise PlanParseError("Not a Trino text plan (expected 'Fragment N [...]' sections).")
    ids = IdCounter()
    fragments: dict[int, PlanNode] = {}
    order: list[int] = []
    analyzed = False

    for number, kind, lines in _split_fragments(text):
        root = _parse_fragment(lines, ids)
        if root is None:
            continue
        root.attrs["fragment"] = f"{number} [{kind}]"
        fragments[number] = root
        order.append(number)

    if not fragments:
        raise PlanParseError("No plan nodes found in Trino output.")

    # Splice fragment trees under their RemoteSource nodes.
    used: set[int] = set()
    for root in fragments.values():
        for n in root.walk():
            if n.op == "RemoteSource":
                for fid in map(int, re.findall(r"\d+", n.attrs.get("sourceFragmentIds", ""))):
                    sub = fragments.get(fid)
                    if sub is not None and fid not in used:
                        n.children.append(sub)
                        used.add(fid)
    top = [fragments[i] for i in order if i not in used]
    root = top[0] if len(top) == 1 else PlanNode(ids.next(), "Plan", children=top)
    analyzed = any(n.actual_rows is not None for n in root.walk())
    return Plan(Engine.TRINO, root, text, analyzed)


def _split_fragments(text: str):
    current: tuple[int, str, list[str]] | None = None
    seen_any = False
    for line in text.splitlines():
        m = _FRAGMENT.match(line)
        if m:
            if current:
                yield current
            current = (int(m.group(1)), m.group(2), [])
            seen_any = True
        elif current is not None:
            current[2].append(line)
    if current:
        yield current
    if not seen_any:  # plain EXPLAIN (LOGICAL) output has no fragments
        yield 0, "LOGICAL", text.splitlines()


def _parse_fragment(lines: list[str], ids: IdCounter) -> PlanNode | None:
    root: PlanNode | None = None
    stack: list[tuple[int, PlanNode]] = []  # (name column, node)
    cur: PlanNode | None = None
    for raw in lines:
        if not raw.strip():
            continue
        m = _CONNECTOR.match(raw)
        if m:
            col = len(m.group(1))
            parent_col = col
            body = m.group(3)
            node = _new_node(body, ids)
            while stack and stack[-1][0] > parent_col:
                stack.pop()
            if not stack:
                continue
            stack[-1][1].children.append(node)
            stack.append((col + 3, node))
            cur = node
            continue
        stripped = raw.strip()
        if root is None:
            r = _ROOT.match(raw.rstrip())
            if r and not stripped.startswith(_HEADER_PREFIXES):
                root = _new_node(stripped, ids)
                stack = [(4, root)]
                cur = root
                continue
            continue
        if cur is not None:
            _apply_detail(cur, raw.lstrip(" │"))
    return root


def _new_node(text: str, ids: IdCounter) -> PlanNode:
    m = re.match(r"^([\w.]+)(?:\[(.*)\])?\s*$", text.strip())
    op, inner = (m.group(1), m.group(2) or "") if m else (text.strip(), "")
    node = PlanNode(ids.next(), op, _kind(op))
    node.attrs["details"] = inner
    low = op.lower()
    if node.kind == SCAN or "scan" in low:
        t = re.search(r"table = ([^\s,\]]+)", inner)
        if t:
            node.relation = _clean_table(t.group(1))
    if node.kind == JOIN:
        node.join_type = {"innerjoin": "INNER", "leftjoin": "LEFT", "rightjoin": "RIGHT", "fulljoin": "FULL",
                          "crossjoin": "CROSS", "semijoin": "SEMI"}.get(low, None)
        t = re.search(r"type = (\w+)", inner)
        if t and node.join_type is None:
            node.join_type = t.group(1).upper()
        node.join_algo = "nested_loop" if low == "crossjoin" else "hash"
        crit = _extract(inner, "criteria")
        node.condition = crit
        node.attrs["has_condition"] = bool(crit) or low == "semijoin"
    flt = _extract(inner, "filterPredicate")
    if flt:
        node.filter = flt
    dyn = _extract(inner, "dynamicFilters")
    if dyn:
        node.attrs["dynamicFilters"] = dyn
    if op == "RemoteSource":
        s = re.search(r"sourceFragmentIds = \[([^\]]*)\]", inner)
        if s:
            node.attrs["sourceFragmentIds"] = s.group(1)
    if "ggregate" in op:
        t = re.search(r"type = (\w+)", inner)
        if t:
            node.attrs["type"] = t.group(1)
        k = re.search(r"keys = \[([^\]]*)\]", inner)
        if k:
            node.attrs["keys"] = k.group(1)
    return node


def _clean_table(raw: str) -> str:
    raw = raw.split("$")[0]
    parts = [p for p in re.split(r"[:.]", raw) if p]
    return ".".join(parts[-3:]) if parts else raw


def _extract(inner: str, key: str) -> str | None:
    m = re.search(rf"{key} = (.+?)(?=, \w+ = |$)", inner)
    return m.group(1).strip() if m else None


def _apply_detail(node: PlanNode, line: str) -> None:
    line = line.strip()
    if line.startswith("Estimates:"):
        first = line.split("}")[0]
        rows = re.search(r"rows: ([^\s,(}]+)", first)
        if rows:
            node.est_rows = _num(rows.group(1))
        for key in ("cpu", "memory", "network"):
            v = re.search(rf"{key}: ([^\s,}}]+)", first)
            if v:
                node.attrs[f"est_{key}"] = v.group(1)
    elif line.startswith("Distribution:"):
        node.attrs["distribution"] = line.split(":", 1)[1].strip()
    elif line.startswith("CPU:"):
        t = re.match(r"CPU:\s*([\d.]+)\s*(ns|us|μs|ms|s|m|h|d)\b", line)
        if t:
            node.self_time_ms = float(t.group(1)) * _TIME[t.group(2)]
        out = re.search(r"Output:\s*([\d.,]+[KMBGT]?) rows?", line)
        if out:
            node.actual_rows = _num(out.group(1))
        inp = re.search(r"Input:\s*([\d.,]+[KMBGT]?) rows?", line)
        if inp:
            node.attrs["input_rows"] = _num(inp.group(1))
    elif line.startswith("Input:"):
        inp = re.search(r"Input:\s*([\d.,]+[KMBGT]?) rows?", line)
        if inp:
            node.attrs["input_rows"] = _num(inp.group(1))
        flt = re.search(r"Filtered:\s*([\d.]+)%", line)
        if flt:
            node.attrs["filtered_pct"] = float(flt.group(1))
    elif ":= " in line and "PARTITION_KEY" in line:
        col = line.split(":=", 1)[0].strip()
        node.attrs.setdefault("partition_keys", []).append(col)
        node.attrs["_last_pk"] = col
    elif line.startswith("::") and node.attrs.get("_last_pk"):
        node.attrs.setdefault("constrained_partition_keys", []).append(node.attrs["_last_pk"])
        node.attrs.pop("_last_pk", None)
    elif line.startswith("Layout:"):
        node.attrs["layout"] = line.split(":", 1)[1].strip()
    elif ":= " in line:
        node.attrs.pop("_last_pk", None)
