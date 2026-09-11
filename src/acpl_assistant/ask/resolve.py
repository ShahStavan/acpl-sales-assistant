"""Entity and period resolution against controlled vocabularies built from the masters.

Handles fuzzy matching (rapidfuzz), fiscal-quarter arithmetic and relative-time anchoring to
the latest data week (2026-06-23). Unknown entity, out-of-period and unsupported-metric
refusals originate here. DESIGN.md §2.4, §2.5.
"""
