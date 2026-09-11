"""Typed representation of the eight playbook rules and their evaluation over FY26.

Thresholds are parsed once from ``action_playbook.xlsx``; ``state`` is read from the
``needs_approval`` column (R-01, R-04, R-08 → ``PENDING_APPROVAL``). DESIGN.md §5.2, §5.3.
"""
