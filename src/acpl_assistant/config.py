"""Runtime configuration read from the environment (see ``.env.example``).

Settings: ``PORT``, ``LLM_PROVIDER``, ``LLM_MODEL``, ``LLM_API_KEY``, ``LLM_BASE_URL``,
``ACPL_DATA_DIR``, ``ACPL_WAREHOUSE``, ``LOG_LEVEL``. The service never reads the OpenCode
build token; the LLM key is the operator's own. DESIGN.md §3.6.
"""
