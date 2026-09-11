"""Evaluation runner: posts every case in ``questions.yaml`` to a running service.

Reports accuracy, median ``cost_usd`` and p50/p95 ``latency_ms`` and writes a timestamped
JSON result under ``eval/results/``. DESIGN.md §6.3.
"""
