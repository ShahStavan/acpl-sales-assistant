"""HTTP client for the configured LLM provider (own key, httpx, JSON-schema responses).

Returns the provider's ``usage`` block with every response so cost is measured, not
estimated. Provider errors surface as ``NO_ANSWER`` with the reason. DESIGN.md §3.4, §6.1.
"""
