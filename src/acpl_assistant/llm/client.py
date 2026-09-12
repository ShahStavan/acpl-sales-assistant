"""HTTP client for the configured LLM provider (own key, httpx, JSON-schema responses).

Returns the provider's ``usage`` block with every response so cost is measured, not
estimated. Provider errors surface as ``NO_ANSWER`` with the reason. DESIGN.md §3.4, §6.1.

Every provider failure leaves this module as a typed :class:`LLMError` carrying a stable
reason token. Nothing from ``httpx`` escapes, which is what lets the pipeline treat a dead
provider as one more ``NO_ANSWER`` rather than as a 500.

One call is attempted against a chain of models rather than a single one: ``LLM_MODEL``
first, then each entry of ``LLM_FALLBACK_MODELS``. A model that reports itself unavailable
— rate-limited, timed out, 5xx, or not found — is stepped over and its fault recorded with
a per-model :class:`~acpl_assistant.llm.breaker.CircuitBreaker`, so a key whose daily
allowance for one model is spent stops paying a round-trip per question to rediscover it.
A fault in the *request* — a 400, a missing key, a body that is not JSON — is never a
reason to fall back: every model in the chain would reject it identically, and trying them
in turn would only multiply one bug into several. DESIGN.md §3.5.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import random
import time
from typing import Any

import httpx

from acpl_assistant.config import Settings
from acpl_assistant.llm.breaker import CircuitBreaker
from acpl_assistant.llm.pricing import cost_usd

logger = logging.getLogger(__name__)

# OpenAI-compatible chat-completions endpoints, by the provider name in ``LLM_PROVIDER``.
# ``LLM_BASE_URL`` overrides whichever is selected.
PROVIDER_BASE_URLS: dict[str, str] = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openai": "https://api.openai.com/v1",
}

CHAT_COMPLETIONS_PATH = "/chat/completions"

# Statuses worth one retry: the provider is rate-limiting or briefly unavailable, and the
# request itself is fine.  A 4xx that is not 429 is a request this code got wrong, and
# sending it again would only spend the budget twice.
RETRYABLE_STATUSES = frozenset({429, 503})

# Sized for a per-minute quota, which clears in tens of seconds: two attempts a second apart
# would report a working provider as unavailable. It cannot rescue a *daily* quota — Gemini's
# free tier caps requests per day per model, and no amount of waiting inside one request will
# clear that — so the ceiling stays low deliberately. A caller that keeps seeing
# ``provider_rate_limited`` is being told to stop, not to wait longer.
#
# Spent only on the *last* model in the chain. While another candidate remains, stepping to
# it is strictly better than sleeping: it costs nothing and it is the one move that can
# still answer the question.
MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_S = 4.0
RETRY_MAX_DELAY_S = 30.0

# Reasons that describe a model as unavailable rather than a request as wrong, and so are
# worth re-asking of the next model in the chain.
FALLBACK_REASONS = frozenset({"provider_rate_limited", "provider_timeout"})

# Both calls are narrow, schema-bound classification and rendering tasks: neither benefits
# from a reasoning budget, and disabling it makes `usage` exact rather than leaving hidden
# thinking tokens out of `completion_tokens` (they are billed as output).
REASONING_EFFORT = "none"
TEMPERATURE = 0.0


class LLMError(Exception):
    """A provider call that did not yield a usable structured response.

    ``reason`` is one of the stable tokens the pipeline reports and the evaluation set
    asserts on, so a refusal caused by the provider is distinguishable from a refusal the
    system chose to make.
    """

    def __init__(self, reason: str, detail: str = "", status: int | None = None) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail
        # The HTTP status behind the failure, where there was one. Carried because it is
        # what separates "this model is unavailable" from "this request is wrong", and only
        # the first of those is worth asking another model.
        self.status = status


@dataclass(frozen=True)
class Usage:
    """Token counts as the provider reported them, with the cost they price to."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    @property
    def billable_output_tokens(self) -> int:
        """Output tokens to price, including any the provider did not itemise.

        Gemini bills reasoning tokens as output but reports them only inside
        ``total_tokens``, so a response can show 14 completion tokens against a total of
        120. Pricing ``completion_tokens`` alone would under-report the cost of exactly
        the calls that cost the most, so the wider of the two figures is used.
        """
        return max(self.completion_tokens, self.total_tokens - self.prompt_tokens)


@dataclass(frozen=True)
class LLMResult:
    """One structured-output call: the parsed JSON object, its usage and its cost."""

    data: dict[str, Any]
    usage: Usage
    model: str
    cost_usd: float


def _usage_from(payload: dict[str, Any]) -> Usage:
    """Read the ``usage`` block, defaulting each count to zero if the provider omits it."""
    usage = payload.get("usage") or {}
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    total = int(usage.get("total_tokens", 0) or 0)
    return Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


def _retry_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped, for attempt *attempt* (1-based)."""
    delay = min(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)), RETRY_MAX_DELAY_S)
    return delay * (0.5 + random.random() / 2)  # noqa: S311  (backoff jitter, not crypto)


def _is_model_fault(exc: LLMError) -> bool:
    """Whether *exc* says the model is unavailable, rather than the request wrong.

    Only the first kind is worth re-asking of another model. A 404 counts — the provider is
    saying it does not serve that model id, which is precisely what a fallback exists for —
    and so does any 5xx. A 400 or a 401 does not: the chain shares one key and one payload
    shape, so every candidate would reject it the same way. Nor does a malformed body,
    which would otherwise let a bug in this module quietly spend the whole chain.
    """
    if exc.reason in FALLBACK_REASONS:
        return True
    if exc.reason != "provider_error" or exc.status is None:
        return False
    return exc.status == httpx.codes.NOT_FOUND or exc.status >= httpx.codes.INTERNAL_SERVER_ERROR


class LLMClient:
    """A chain of models behind one provider, reached over one pooled ``httpx`` connection.

    Constructed once per process and shared: opening a connection per call would show up
    in the latency the endpoint reports, and the endpoint reports what the caller waits.
    The breaker is shared for the same reason — a per-request breaker would forget the
    outage between two questions and so never prevent anything.
    """

    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._settings = settings
        self._model = settings.LLM_MODEL
        self._chain = settings.llm_model_chain
        base = settings.LLM_BASE_URL.strip() or PROVIDER_BASE_URLS.get(
            settings.LLM_PROVIDER.strip().casefold(), ""
        )
        self._base_url = base.rstrip("/")
        self._breaker = breaker or CircuitBreaker(
            threshold=settings.LLM_BREAKER_THRESHOLD,
            cooldown_s=float(settings.LLM_BREAKER_COOLDOWN_S),
        )
        self._client = httpx.Client(
            timeout=httpx.Timeout(float(settings.LLM_TIMEOUT_S)),
            transport=transport,
        )

    @property
    def model(self) -> str:
        """The primary model: what a healthy call is sent to and priced against."""
        return self._model

    @property
    def models(self) -> tuple[str, ...]:
        """The full chain, primary first, in the order a call tries them."""
        return self._chain

    @property
    def fallback_models(self) -> tuple[str, ...]:
        """The chain after the primary — what a call degrades to, in order."""
        return self._chain[1:]

    @property
    def degraded_models(self) -> list[str]:
        """Models whose breaker is currently open, so calls are skipping them."""
        return self._breaker.open_models()

    @property
    def configured(self) -> bool:
        """Whether a key and an endpoint are both present, so a call could be attempted."""
        return bool(self._settings.LLM_API_KEY.strip()) and bool(self._base_url)

    def close(self) -> None:
        """Close the pooled connection at process shutdown."""
        self._client.close()

    # -- the one call shape this service makes -----------------------------

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        max_tokens: int = 1024,
    ) -> LLMResult:
        """Ask the provider for one JSON object conforming to *schema*.

        Tries each live model in the chain in order and returns the first structured
        response, so ``LLMResult.model`` names the model that actually answered and prices
        the call — never the one that was asked for first.

        Raises:
            LLMError: for every failure — no key, transport, status, unparseable body or a
                response that is not a JSON object. When the chain was exhausted the error
                is the last model's. The caller never sees an ``httpx`` exception, so no
                provider fault can become a 500.
        """
        if not self._settings.LLM_API_KEY.strip():
            raise LLMError("no_provider_key", "LLM_API_KEY is not set")
        if not self._base_url:
            raise LLMError(
                "provider_error", f"no endpoint for provider {self._settings.LLM_PROVIDER!r}"
            )
        if not self._chain:
            raise LLMError("provider_error", "no model configured; set LLM_MODEL")

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        candidates = self._candidates()
        last: LLMError | None = None

        for position, model in enumerate(candidates):
            final = position == len(candidates) - 1
            try:
                result = self._call(model, messages, schema_name, schema, max_tokens, retry=final)
            except LLMError as exc:
                if not _is_model_fault(exc):
                    raise
                self._breaker.record_failure(model)
                last = exc
                if not final:
                    logger.warning(
                        "model %r unavailable (%s); falling back to %r",
                        model,
                        exc.reason,
                        candidates[position + 1],
                    )
                continue
            self._breaker.record_success(model)
            return result

        if last is not None:
            raise last
        # Unreachable: ``_candidates`` never returns an empty tuple, so the loop above
        # either returned a result or recorded a failure.
        raise LLMError("provider_error", "no model was tried")  # pragma: no cover

    def _candidates(self) -> tuple[str, ...]:
        """The chain minus the models currently in cooldown — never empty.

        A chain whose every breaker is open still yields one candidate. Refusing without a
        call would make the cooldown itself the outage, and it is exactly the call the
        breaker lets through that closes it again.
        """
        live = tuple(model for model in self._chain if not self._breaker.is_open(model))
        return live or (self._chain[0],)

    # -- transport ---------------------------------------------------------

    def _call(
        self,
        model: str,
        messages: list[dict[str, str]],
        schema_name: str,
        schema: dict[str, Any],
        max_tokens: int,
        *,
        retry: bool,
    ) -> LLMResult:
        """Send one request to *model* and return its parsed, priced result."""
        payload = {
            "model": model,
            "messages": messages,
            "temperature": TEMPERATURE,
            "reasoning_effort": REASONING_EFFORT,
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        }
        body = self._post_with_retry(payload, MAX_ATTEMPTS if retry else 1)
        return self._parse(body, model)

    def _post_with_retry(self, payload: dict[str, Any], attempts: int) -> dict[str, Any]:
        """POST the payload, retrying a rate limit or brief outage up to *attempts* times.

        *attempts* is one for every model that still has a fallback behind it: waiting out a
        quota is worth doing only when there is nothing left to step across to.
        """
        last: LLMError | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._post_once(payload)
            except LLMError as exc:
                last = exc
                if exc.reason != "provider_rate_limited" or attempt == attempts:
                    raise
                delay = _retry_delay(attempt)
                logger.warning("provider rate-limited; retrying in %.1fs", delay)
                time.sleep(delay)
        raise last  # pragma: no cover - the loop either returns or raises

    def _post_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one request and return the decoded response body."""
        try:
            response = self._client.post(
                f"{self._base_url}{CHAT_COMPLETIONS_PATH}",
                headers={
                    "Authorization": f"Bearer {self._settings.LLM_API_KEY.strip()}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise LLMError(
                "provider_timeout", f"no response in {self._settings.LLM_TIMEOUT_S}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError("provider_error", exc.__class__.__name__) from exc

        if response.status_code in RETRYABLE_STATUSES:
            raise LLMError(
                "provider_rate_limited", f"HTTP {response.status_code}", response.status_code
            )
        if response.status_code >= httpx.codes.BAD_REQUEST:
            # The body can carry the key back in an error echo, so only the status travels.
            raise LLMError("provider_error", f"HTTP {response.status_code}", response.status_code)

        try:
            return response.json()
        except ValueError as exc:
            raise LLMError("provider_error", "response body was not JSON") from exc

    def _parse(self, body: dict[str, Any], model: str) -> LLMResult:
        """Pull the JSON object out of the first choice, priced against *model*."""
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("provider_error", "response carried no message content") from exc

        try:
            data = json.loads(content)
        except (TypeError, ValueError) as exc:
            raise LLMError("provider_error", "model output was not valid JSON") from exc
        if not isinstance(data, dict):
            raise LLMError("provider_error", "model output was not a JSON object")

        # Priced against the model this client *asked*, which after a fallback is not the
        # primary. Not against the id echoed in the body: a provider that silently serves a
        # different model must not also choose which rate card the request is billed at.
        usage = _usage_from(body)
        return LLMResult(
            data=data,
            usage=usage,
            model=model,
            cost_usd=cost_usd(model, usage.prompt_tokens, usage.billable_output_tokens),
        )
