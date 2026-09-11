"""``acpl-prepare`` / ``python prepare.py`` command-line entry point.

Runs the preparation pipeline end to end and writes ``warehouse.duckdb`` and
``prep_report.json``. Never modifies the provided data pack. DESIGN.md §3.2.
"""
