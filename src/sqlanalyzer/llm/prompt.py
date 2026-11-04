"""Prompt construction and answer parsing for the LLM optimizer."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..models import Finding
from ..optimizer import OptimizationResult

SYSTEM = (
    "You are a senior SQL performance engineer. You rewrite queries so they run faster on the given engine "
    "WITHOUT changing the result set: same columns, same column order, same rows, same ordering semantics. "
    "You never invent tables or columns that are not in the original query or the provided schema. "
    "If nothing can be improved, return the original query unchanged. "
    "Answer with one JSON object and nothing else."
)

_RESPONSE_SHAPE = """{
  "sql": "<the rewritten query, valid {dialect} SQL>",
  "changes": [
    {"title": "<short name of the change>", "reason": "<why it is faster>", "risk": "<none|result-may-change>"}
  ],
  "notes": "<anything the user should check, or an empty string>"
}"""

MAX_FINDINGS = 12


@dataclass
class LlmSuggestion:
    """What the model answered, after parsing."""

    sql: str
    changes: list[dict] = field(default_factory=list)
    notes: str = ""
    raw: str = ""
    parse_error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.sql.strip())

    def change_titles(self) -> list[str]:
        return [str(c.get("title") or "").strip() for c in self.changes if c.get("title")]


def build_prompt(sql: str, dialect: str, *, schema_text: str = "", findings: list[Finding] | None = None,
                 rules: OptimizationResult | None = None, partition_text: str = "") -> str:
    """User message: the query, its context and what the static analysis already knows."""
    parts = [f"Engine / SQL dialect: {dialect}.", "", "Query to optimize:", "```sql", sql.strip(), "```"]
    if schema_text.strip():
        parts += ["", "Declared schema (table: column type, ...):", "```", schema_text.strip(), "```"]
    else:
        parts += ["", "No schema was declared: do not expand SELECT * and do not assume column types."]
    if partition_text.strip():
        parts += ["", "Partition columns (filters on them enable partition pruning):", "```",
                  partition_text.strip(), "```"]
    shown = [f for f in (findings or []) if f.line is not None][:MAX_FINDINGS]
    if shown:
        parts += ["", "Problems the static analyzer already found (line numbers refer to the query above):"]
        parts += [f"- line {f.line}: [{f.rule}] {f.message} {f.suggestion}".strip() for f in shown]
    if rules is not None and rules.changed:
        parts += ["", "A deterministic rule engine produced this rewrite; you may improve on it, but every "
                      "rewrite it applied is known to be correct:", "```sql", rules.optimized_sql.strip(), "```",
                  "Rules applied: " + ", ".join(sorted({rw.rule for rw in rules.rewrites})) + "."]
    parts += [
        "",
        "Rules you must follow:",
        "1. Keep the result identical: same projected columns in the same order, same rows, same ORDER BY.",
        "2. Only use tables, columns and CTEs that appear in the query above (or in the declared schema).",
        "3. Never drop a filter, a join, a GROUP BY key, DISTINCT, LIMIT or an aggregate.",
        "4. Prefer: sargable predicates (no functions on columns), explicit JOIN ... ON, early filtering, "
        "removing redundant DISTINCT/ORDER BY, anti-joins instead of NOT IN, UNION ALL when duplicates are "
        "impossible. Mark any change that can alter results with risk \"result-may-change\".",
        "5. Output valid " + dialect + " SQL, formatted over multiple lines.",
        "",
        "Answer with exactly this JSON object:",
        _RESPONSE_SHAPE.replace("{dialect}", dialect),
    ]
    return "\n".join(parts)


# ------------------------------------------------------------------------ parsing
_FENCE = re.compile(r"```(?:sql|json)?\s*(.*?)```", re.S | re.I)


def parse_response(text: str) -> LlmSuggestion:
    """Extract SQL (and the change list) from whatever the model answered.

    Accepts a clean JSON object, JSON inside a fenced block, JSON with leading
    chatter, or a bare ```sql``` block.
    """
    raw = text or ""
    data = _first_json_object(raw)
    if isinstance(data, dict) and isinstance(data.get("sql"), str):
        changes = data.get("changes") or []
        if not isinstance(changes, list):
            changes = []
        changes = [c if isinstance(c, dict) else {"title": str(c)} for c in changes]
        notes = data.get("notes")
        return LlmSuggestion(_clean_sql(data["sql"]), changes, str(notes or ""), raw)

    for block in _FENCE.findall(raw):
        candidate = block.strip()
        if candidate.lower().startswith(("select", "with", "insert", "update", "delete", "create", "(")):
            return LlmSuggestion(_clean_sql(candidate), [], "", raw,
                                 "The answer was not JSON; the SQL block was used.")
    stripped = raw.strip()
    if stripped.lower().startswith(("select", "with")):
        return LlmSuggestion(_clean_sql(stripped), [], "", raw, "The answer was not JSON; the whole reply was used.")
    return LlmSuggestion("", [], "", raw, "No SQL found in the model's answer.")


def _first_json_object(text: str) -> dict | None:
    candidates = [m.strip() for m in _FENCE.findall(text)] + [text.strip()]
    for candidate in candidates:
        start = candidate.find("{")
        if start < 0:
            continue
        depth, in_string, escape = 0, False, False
        for i, ch in enumerate(candidate[start:], start):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[start:i + 1])
                    except json.JSONDecodeError:
                        break
    return None


def _clean_sql(sql: str) -> str:
    sql = sql.strip()
    match = _FENCE.search(sql)
    if match:
        sql = match.group(1).strip()
    return sql.strip().rstrip(";").strip() + (";" if sql.strip().endswith(";") else "")
