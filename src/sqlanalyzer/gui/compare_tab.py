"""Compare two versions of a query: findings, complexity and execution plans."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..analyzer import Comparison, analyze_query, compare_queries
from ..db import fetch_plan_text
from ..plans.diff import ADDED, CHANGED, REMOVED, SAME
from .editor import CodeEditor
from .paste_dialog import PastePlanDialog
from .plan_tab import show_plan_graph
from .session import Session
from .svg_view import SvgView
from .workers import run_async

_COLORS = {ADDED: "#1a7f37", REMOVED: "#d73027", CHANGED: "#bf8700", SAME: "#57606a"}
_SYMBOL = {ADDED: "+", REMOVED: "−", CHANGED: "~", SAME: " "}


class _Side(QWidget):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.plan_text = ""
        self.editor = CodeEditor(f"{title} query…")
        self.plan_btn = QPushButton("Set plan…")
        self.plan_btn.setToolTip("Optionally paste an EXPLAIN output instead of fetching it from the database")
        self.plan_label = QLabel("")
        self.plan_label.setStyleSheet("color: gray;")
        header = QHBoxLayout()
        header.addWidget(QLabel(f"<b>{title}</b>"))
        header.addStretch(1)
        header.addWidget(self.plan_label)
        header.addWidget(self.plan_btn)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addWidget(self.editor, 1)

    def set_plan_text(self, text: str) -> None:
        self.plan_text = text
        self.plan_label.setText("pasted plan" if text else "")


class CompareTab(QWidget):
    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        self.old, self.new = _Side("Version A (before)"), _Side("Version B (after)")
        self.old.plan_btn.clicked.connect(lambda: self._paste(self.old))
        self.new.plan_btn.clicked.connect(lambda: self._paste(self.new))

        self.run_btn = QPushButton("Compare")
        self.run_btn.clicked.connect(self.run)
        self.analyze_box = QCheckBox("ANALYZE (executes both queries)")
        self.status = QLabel("")

        self.summary = QTextBrowser()
        self.summary.setMaximumHeight(170)
        self.diff_tree = QTreeWidget()
        self.diff_tree.setHeaderLabels(["Operator", "Changes"])
        self.diff_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.old_view, self.new_view = SvgView(), SvgView()
        for v in (self.old_view, self.new_view):
            v.show_message("Plan graph appears after comparison.")
        graphs = QSplitter(Qt.Orientation.Horizontal)
        for title, v in (("A", self.old_view), ("B", self.new_view)):
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.addWidget(QLabel(f"Plan {title}  (green = added · red = removed · amber = changed)"))
            bl.addWidget(v, 1)
            graphs.addWidget(box)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.diff_tree, "Operator diff")
        self.tabs.addTab(graphs, "Plan graphs")
        self.findings_view = QTextBrowser()
        self.tabs.addTab(self.findings_view, "Findings")

        editors = QSplitter(Qt.Orientation.Horizontal)
        editors.addWidget(self.old)
        editors.addWidget(self.new)
        results = QWidget()
        rl = QVBoxLayout(results)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.summary)
        rl.addWidget(self.tabs, 1)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(editors)
        split.addWidget(results)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)

        bar = QHBoxLayout()
        bar.addWidget(self.run_btn)
        bar.addWidget(self.analyze_box)
        bar.addWidget(self.status, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(split, 1)

    # ---------------------------------------------------------------------------
    def use_queries(self, old_sql: str, new_sql: str) -> None:
        self.old.editor.setPlainText(old_sql)
        self.new.editor.setPlainText(new_sql)

    def _paste(self, side: _Side) -> None:
        dlg = PastePlanDialog(self.session.engine, side.plan_text, self)
        if dlg.exec():
            side.set_plan_text(dlg.plan_text())

    def run(self) -> None:
        sqls = (self.old.editor.toPlainText(), self.new.editor.toPlainText())
        texts = (self.old.plan_text, self.new.plan_text)
        cfg = self.session.connection
        analyze = self.analyze_box.isChecked()
        fetch = [cfg is not None and not t for t in texts]
        if cfg is None or not any(fetch):
            self._finish(sqls, texts)
            return
        connection = cfg

        self.run_btn.setEnabled(False)
        self.status.setText("Running EXPLAIN…")

        def work() -> tuple[str, str]:
            old_sql, new_sql = sqls
            old_text, new_text = texts
            return (fetch_plan_text(connection, old_sql, analyze) if fetch[0] else old_text,
                    fetch_plan_text(connection, new_sql, analyze) if fetch[1] else new_text)

        def done(result: tuple[str, str]) -> None:
            self.run_btn.setEnabled(True)
            self._finish(sqls, result)

        def failed(msg: str) -> None:
            self.run_btn.setEnabled(True)
            self.status.setText(f"EXPLAIN failed: {msg}")
            self._finish(sqls, texts)  # still show the static comparison

        run_async(self, work, done, failed)

    def _finish(self, sqls: tuple[str, str], plan_texts: tuple[str, str]) -> None:
        engine, hints = self.session.engine, self.session.hints
        stats, schema = self.session.stats, self.session.schema
        old = analyze_query(sqls[0], engine, hints, plan_texts[0] or None, schema=schema, stats=stats, optimize=False)
        new = analyze_query(sqls[1], engine, hints, plan_texts[1] or None, schema=schema, stats=stats, optimize=False)
        cmp = compare_queries(old, new)
        self._render(cmp)
        if not self.status.text().startswith("EXPLAIN failed"):
            if cmp.plan_diff is None:
                self.status.setText("Static comparison only (no plans available).")
            else:
                self.status.setText("Offline plans compared (derived from the SQL)." if cmp.offline_plans
                                    else "Plans compared.")

    def _render(self, cmp: Comparison) -> None:
        lines: list[str] = []
        o, n = cmp.old.complexity, cmp.new.complexity
        if cmp.complexity_delta is not None and o is not None and n is not None:
            lines.append(f"Complexity: {o.score} ({o.level}) → {n.score} ({n.level}) [{cmp.complexity_delta:+d}]")
        lines.append(f"Findings: {len(cmp.old.findings)} → {len(cmp.new.findings)} "
                     f"({len(cmp.resolved)} resolved, {len(cmp.introduced)} introduced)")
        if cmp.cost_change:
            lines.append(f"Offline cost model: {cmp.cost_change}")
        if cmp.plan_diff:
            if cmp.offline_plans:
                lines.append("Operator diff of the plans derived from the SQL (no database was contacted); "
                             "connect or use “Set plan…” for measured plans.")
            lines += cmp.plan_diff.summary
        elif cmp.old.effective_plan is None or cmp.new.effective_plan is None:
            lines.append("No execution plans: fix the syntax errors, connect to a database, "
                         "or use “Set plan…”.")
        self.summary.setHtml("<ul>" + "".join(f"<li>{_esc(x)}</li>" for x in lines) + "</ul>")

        self.diff_tree.clear()
        old_view_status = new_view_status = None
        if cmp.plan_diff:
            stack: list[tuple[int, QTreeWidgetItem]] = []
            for e in cmp.plan_diff.entries:
                item = QTreeWidgetItem([f"{_SYMBOL[e.status]} {e.label}", "; ".join(e.changes)])
                item.setForeground(0, QBrush(QColor(_COLORS[e.status])))
                while stack and stack[-1][0] >= e.depth:
                    stack.pop()
                (stack[-1][1].addChild(item) if stack else self.diff_tree.addTopLevelItem(item))
                stack.append((e.depth, item))
            self.diff_tree.expandAll()
            old_view_status, new_view_status = cmp.plan_diff.old_ids, cmp.plan_diff.new_ids

        show_plan_graph(self.old_view, cmp.old.effective_plan, status=old_view_status)
        show_plan_graph(self.new_view, cmp.new.effective_plan, status=new_view_status)

        def block(title: str, color: str, fs) -> str:
            if not fs:
                return ""
            items = "".join(f"<li>[{f.severity.value}] {_esc(f.message)}</li>" for f in fs)
            return f"<h4 style='color:{color}'>{title} ({len(fs)})</h4><ul>{items}</ul>"

        self.findings_view.setHtml(
            block("Resolved in B", _COLORS[ADDED], cmp.resolved)
            + block("Introduced in B", _COLORS[REMOVED], cmp.introduced)
            or "<p>No difference in findings.</p>")
        self.tabs.setCurrentIndex(0 if cmp.plan_diff else 2)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

