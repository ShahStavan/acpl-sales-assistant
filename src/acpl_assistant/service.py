"""FastAPI application and process entry point.

Exposes ``POST /ask`` and ``POST /actions``; ``main()`` runs uvicorn on ``PORT``. The service
opens the DuckDB warehouse read-only and has no write path or outbound channel other than the
LLM provider. DESIGN.md §3.2.
"""
