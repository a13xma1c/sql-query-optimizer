"""Unit tests for the finding-rule catalog and the post-hoc finding filter."""

from __future__ import annotations

from sqlanalyzer.analyzer import analyze_query
from sqlanalyzer.dialects import Engine
from sqlanalyzer.models import Category, Finding, Severity
from sqlanalyzer.rules_registry import ALL_RULES, filter_findings, rule_info, rules_by_category


def test_every_rule_name_is_unique():
    names = [r.name for r in ALL_RULES]
    assert len(names) == len(set(names))


def test_rule_info_looks_up_known_and_unknown_rules():
    info = rule_info("select-star")
    assert info is not None and info.category is Category.LINT
    assert rule_info("not-a-real-rule") is None


def test_rules_by_category_covers_every_category_used():
    grouped = rules_by_category()
    assert Category.LINT in grouped and Category.JOIN in grouped and Category.SCAN in grouped
    assert Category.SCHEMA in grouped and Category.PARTITION in grouped and Category.ENGINE in grouped
    assert Category.PLAN in grouped and Category.COMPLEXITY in grouped
    for cat, rules in grouped.items():
        assert all(r.category == cat for r in rules)


def test_engine_specific_rules_carry_their_engine_restriction():
    info = rule_info("count-distinct-exact")
    assert info is not None
    assert Engine.TRINO in info.engines and Engine.POSTGRES not in info.engines
    assert info.engines != frozenset(Engine)  # not every engine

    pg_only = rule_info("cte-materialized")
    assert pg_only is not None and pg_only.engines == frozenset({Engine.POSTGRES})


def test_filter_findings_drops_only_the_named_rules():
    findings = [
        Finding("select-star", Severity.INFO, Category.LINT, "a"),
        Finding("missing-where", Severity.WARNING, Category.LINT, "b"),
    ]
    assert filter_findings(findings, []) == findings
    kept = filter_findings(findings, {"select-star"})
    assert [f.rule for f in kept] == ["missing-where"]
    assert filter_findings(findings, {"unknown-rule"}) == findings


def test_analyze_query_honours_disabled_finding_rules():
    sql = "SELECT * FROM orders"
    a = analyze_query(sql, Engine.POSTGRES)
    assert any(f.rule == "select-star" for f in a.findings)
    b = analyze_query(sql, Engine.POSTGRES, disabled_finding_rules={"select-star"})
    assert not any(f.rule == "select-star" for f in b.findings)
    # every other finding (if any) survives untouched
    assert {f.rule for f in b.findings} == {f.rule for f in a.findings} - {"select-star"}


def test_every_rule_triggered_by_the_bundled_samples_is_registered():
    from sqlanalyzer.samples import load_samples

    unknown = set()
    for sample in load_samples():
        for engine in sample.engines:
            analysis = analyze_query(sample.sql, engine, optimize=False)
            for f in analysis.findings:
                if rule_info(f.rule) is None:
                    unknown.add(f.rule)
    assert not unknown
