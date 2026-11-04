"""Context-aware SQL autocomplete sourced from SQL keywords and the declared/imported schema.

The candidate logic here is plain Python (independent of Qt) so it can be unit-tested without a
running application; :func:`attach_completer` is the only piece that touches Qt, wiring a
``QCompleter`` onto a :class:`~sqlanalyzer.gui.editor.CodeEditor` via the hook it exposes.

Typing ``alias.`` after a table reference such as ``FROM orders o`` (or ``JOIN ... AS o``) offers
that table's columns, found through a tolerant regex scan that does not require the SQL typed so
far to actually parse. Anywhere else, completions are keywords + known table names + every column
across every declared table (good enough before a ``FROM`` clause narrows things down).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from ..optimizer.schema import SchemaHints
from .editor import KEYWORDS

_WORD = r"[A-Za-z_][A-Za-z0-9_$]*"
_TABLE_REF = re.compile(rf"\b(?:from|join)\s+({_WORD}(?:\.{_WORD})*)(?:\s+(?:as\s+)?({_WORD}))?", re.IGNORECASE)
_TRAILING_QUALIFIED = re.compile(rf"({_WORD})\.({_WORD})?$")
_TRAILING_WORD = re.compile(rf"({_WORD})$")
_KEYWORD_SET = {kw.lower() for kw in KEYWORDS}


def table_aliases(sql: str) -> dict[str, str]:
    """Best-effort ``alias|table -> table`` map, tolerant of invalid/incomplete SQL.

    Every referenced table also maps to itself (so ``orders.`` works even without an alias).
    """
    aliases: dict[str, str] = {}
    for m in _TABLE_REF.finditer(sql):
        table, alias = m.group(1), m.group(2)
        table_key = table.split(".")[-1].lower()
        aliases[table_key] = table.lower()
        if alias and alias.lower() not in _KEYWORD_SET:
            aliases[alias.lower()] = table.lower()
    return aliases


def columns_for(hints: SchemaHints | None, table: str) -> list[str]:
    """Declared columns of ``table`` (matched loosely against the schema's qualification)."""
    if hints is None:
        return []
    table = table.lower()
    for name, cols in hints.tables.items():
        if name == table or name.endswith("." + table) or table.endswith("." + name):
            return list(cols)
    return []


def context(text_before_cursor: str) -> tuple[str | None, str]:
    """``(qualifier, prefix)`` of the identifier being typed right before the cursor.

    ``qualifier`` is the alias/table name before a ``.``, or ``None`` when there isn't one.
    """
    m = _TRAILING_QUALIFIED.search(text_before_cursor)
    if m:
        return m.group(1), m.group(2) or ""
    m = _TRAILING_WORD.search(text_before_cursor)
    return None, m.group(1) if m else ""


def candidates(hints: SchemaHints | None, sql: str, text_before_cursor: str) -> tuple[str, list[str]]:
    """``(prefix, options)`` ready for a ``QCompleter``'s completion model."""
    qualifier, prefix = context(text_before_cursor)
    if qualifier:
        table = table_aliases(sql).get(qualifier.lower(), qualifier.lower())
        return prefix, sorted(columns_for(hints, table))
    tables = list(hints.tables) if hints else []
    bare_tables = {t.split(".")[-1] for t in tables}
    all_columns: set[str] = set()
    for cols in (hints.tables.values() if hints else ()):
        all_columns.update(cols)
    return prefix, sorted({*KEYWORDS, *bare_tables, *all_columns})


def attach_completer(editor, schema: Callable[[], SchemaHints | None]) -> None:
    """Wire a popup ``QCompleter`` onto ``editor`` that consults ``schema()`` on every keystroke."""
    from PySide6.QtWidgets import QCompleter

    def provide(sql: str, text_before_cursor: str) -> tuple[str, list[str]]:
        return candidates(schema(), sql, text_before_cursor)

    editor.set_completer(QCompleter(editor), provide)
