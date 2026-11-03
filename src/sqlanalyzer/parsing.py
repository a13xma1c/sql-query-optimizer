"""Syntax validation and formatting."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import ErrorLevel, exp
from sqlglot.errors import ParseError, SqlglotError, TokenError

from .models import Category, Finding, Severity


@dataclass
class ParseResult:
    statements: list[exp.Expression] = field(default_factory=list)
    errors: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def first(self) -> exp.Expression | None:
        return self.statements[0] if self.statements else None


_TOKEN_REPR = re.compile(r"<Token token_type: [\w.]+, text: (.*?), line: \d+, col: \d+.*?>")
_TOKEN_POS = re.compile(r"line (\d+), col (\d+)", re.IGNORECASE)


def parse_sql(sql: str, dialect: str) -> ParseResult:
    """Parse ``sql`` and convert any failure into :class:`Finding` objects."""
    result = ParseResult()
    if not sql.strip():
        result.errors.append(
            Finding("empty-query", Severity.ERROR, Category.SYNTAX, "The query is empty.")
        )
        return result
    try:
        parsed = sqlglot.parse(sql, dialect=dialect, error_level=ErrorLevel.RAISE)
    except ParseError as exc:
        seen: set[tuple] = set()
        for err in exc.errors or [{"description": str(exc)}]:
            desc = _TOKEN_REPR.sub(lambda m: f"'{m.group(1)}'", str(err.get("description", exc)).strip())
            key = (err.get("line"), err.get("col"), desc)
            if key in seen:
                continue
            seen.add(key)
            highlight = err.get("highlight")
            msg = desc + (f" near '{highlight}'" if highlight else "")
            result.errors.append(
                Finding("syntax-error", Severity.ERROR, Category.SYNTAX, msg,
                        line=err.get("line"), col=err.get("col"))
            )
        return result
    except TokenError as exc:
        m = _TOKEN_POS.search(str(exc))
        line, col = (int(m.group(1)), int(m.group(2))) if m else _tokenizer_position(sql)
        result.errors.append(
            Finding("tokenize-error", Severity.ERROR, Category.SYNTAX,
                    f"Cannot tokenize SQL (unterminated string, comment or identifier?): {exc}",
                    line=line, col=col)
        )
        return result
    except SqlglotError as exc:  # pragma: no cover - defensive
        result.errors.append(Finding("syntax-error", Severity.ERROR, Category.SYNTAX, str(exc)))
        return result

    result.statements = [s for s in parsed if s is not None]
    if not result.statements:
        result.errors.append(
            Finding("empty-query", Severity.ERROR, Category.SYNTAX, "The query contains no statements.")
        )
    return result


def _tokenizer_position(sql: str) -> tuple[int | None, int | None]:
    """Locate the opening of an unterminated quote/comment for TokenError."""
    for pattern in (r"/\*", r"'", r'"'):
        stripped = sql
        positions = [m.start() for m in re.finditer(pattern, stripped)]
        if pattern != r"/\*" and len(positions) % 2 == 0:
            continue
        if pattern == r"/\*" and "*/" in sql:
            continue
        if positions:
            pos = positions[-1]
            line = sql.count("\n", 0, pos) + 1
            col = pos - (sql.rfind("\n", 0, pos) + 1) + 1
            return line, col
    return None, None


def format_sql(sql: str, dialect: str, *, indent: int = 2) -> str:
    """Pretty-print ``sql``. Raises ``SqlglotError`` for invalid SQL."""
    statements = sqlglot.parse(sql, dialect=dialect, error_level=ErrorLevel.RAISE)
    pieces = [
        s.sql(dialect=dialect, pretty=True, indent=indent, comments=True)
        for s in statements
        if s is not None
    ]
    return ";\n\n".join(pieces) + (";" if pieces else "")
