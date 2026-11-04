#!/usr/bin/env python
"""Populate the SQL Analyzer demo environment.

Examples::

    python scripts/populate_demo.py duckdb                         # ~/.config/SQLAnalyzer/demo.duckdb
    python scripts/populate_demo.py duckdb ./demo.duckdb --scale 0.2
    python scripts/populate_demo.py postgres postgresql+psycopg2://postgres@localhost:5432/demo
    python scripts/populate_demo.py ddl trino > scripts/demo_trino.sql
    python scripts/populate_demo.py schema                         # schema hints for the Schema tab

This is a thin wrapper around ``python -m sqlanalyzer.demo``; both work the same.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlanalyzer.demo import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
