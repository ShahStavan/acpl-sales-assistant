"""Request-scoped meter for cost and timing.

Accumulates provider token usage into ``cost_usd`` and stage timings into ``timings_ms``;
``latency_ms`` spans the whole handler via ``perf_counter_ns``. DESIGN.md §6.1–§6.2.
"""
