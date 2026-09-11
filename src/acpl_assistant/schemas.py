"""Pydantic request and response models for the HTTP contract.

``/ask``: ``{question}`` → ``{answer, status, evidence, cost_usd, latency_ms}`` plus the
documented extra fields ``timings_ms`` and ``intent``. ``/actions``: ``{scope}`` → a list of
``{finding, rule_id, action, state}`` plus ``period``, ``evidence``, ``priority``.
DESIGN.md Appendix A.
"""
