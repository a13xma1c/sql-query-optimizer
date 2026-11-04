"""Unit tests for SQL autocomplete candidate logic (no Qt involved)."""

from __future__ import annotations

from sqlanalyzer.gui.completer import candidates, columns_for, context, table_aliases
from sqlanalyzer.optimizer.schema import parse_schema


def test_table_aliases_picks_up_from_and_join_with_and_without_as():
    sql = "SELECT * FROM orders o JOIN customers AS c ON o.customer_id = c.id"
    aliases = table_aliases(sql)
    assert aliases["o"] == "orders" and aliases["c"] == "customers"
    assert aliases["orders"] == "orders" and aliases["customers"] == "customers"


def test_table_aliases_ignores_keywords_as_alias():
    sql = "SELECT * FROM orders WHERE id = 1"
    aliases = table_aliases(sql)
    assert "where" not in aliases
    assert aliases["orders"] == "orders"


def test_table_aliases_is_tolerant_of_incomplete_sql():
    sql = "SELECT o.id, o. FROM orders o JOIN cus"
    aliases = table_aliases(sql)
    assert aliases["o"] == "orders"


def test_context_detects_qualifier_and_prefix():
    assert context("SELECT o.cust") == ("o", "cust")
    assert context("SELECT o.") == ("o", "")
    assert context("SELECT cus") == (None, "cus")
    assert context("SELECT ") == (None, "")
    assert context("") == (None, "")


def test_columns_for_matches_loosely_qualified_names():
    hints = parse_schema("orders: id int, customer_id int\ncustomers: id int, name varchar")
    assert columns_for(hints, "orders") == ["id", "customer_id"]
    assert columns_for(hints, "customers") == ["id", "name"]
    assert columns_for(hints, "missing") == []
    assert columns_for(None, "orders") == []


def test_columns_for_matches_across_qualification_depth():
    hints = parse_schema("sales.orders: id int, customer_id int")
    assert columns_for(hints, "orders") == ["id", "customer_id"]
    assert columns_for(hints, "sales.orders") == ["id", "customer_id"]


def test_candidates_after_alias_dot_offers_only_that_tables_columns():
    hints = parse_schema("orders: id int, customer_id int\ncustomers: id int, name varchar")
    sql = "SELECT o. FROM orders o JOIN customers c ON o.customer_id = c.id"
    prefix, options = candidates(hints, sql, "SELECT o.")
    assert prefix == "" and options == ["customer_id", "id"]


def test_candidates_with_prefix_after_dot_is_kept_for_filtering():
    hints = parse_schema("orders: id int, customer_id int")
    prefix, options = candidates(hints, "SELECT o.cust FROM orders o", "SELECT o.cust")
    assert prefix == "cust" and "customer_id" in options


def test_candidates_without_qualifier_offers_keywords_tables_and_columns():
    hints = parse_schema("orders: id int, customer_id int")
    prefix, options = candidates(hints, "SELECT * FROM ord", "SELECT * FROM ord")
    assert prefix == "ord"
    assert "orders" in options and "customer_id" in options and "select" in options


def test_candidates_with_no_schema_still_offers_keywords():
    prefix, options = candidates(None, "SEL", "SEL")
    assert prefix == "SEL" and "select" in options
