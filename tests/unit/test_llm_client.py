"""The provider client, over ``httpx.MockTransport`` — no key, no network, no billing.

Every branch here exists to keep one promise: nothing from ``httpx`` escapes this module.
A transport error, a rate limit, an HTML error page and a model that answers off schema all
have to leave as an :class:`LLMError` with a stable reason, because the pipeline turns those
reasons into ``NO_ANSWER`` and anything else into a 500.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from acpl_assistant.config import Settings
from acpl_assistant.llm import client as client_module
from acpl_assistant.llm.client import (
    MAX_ATTEMPTS,
    PROVIDER_BASE_URLS,
    LLMClient,
    LLMError,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"intent": {"type": "string"}},
    "required": ["intent"],
    "additionalProperties": False,
}

KEY = "test-key-not-a-real-one"


def settings(**overrides: Any) -> Settings:
    """Settings with a key present, so the no-key branch is only taken deliberately."""
    base: dict[str, Any] = {
        "LLM_API_KEY": KEY,
        "LLM_PROVIDER": "gemini",
        "LLM_MODEL": "gemini-2.5-flash",
        "LLM_BASE_URL": "",
        "LLM_TIMEOUT_S": 5,
    }
    return Settings(**{**base, **overrides})


def completion(content: str, usage: dict[str, int] | None = None) -> dict[str, Any]:
    """An OpenAI-compatible chat-completions body carrying *content*."""
    return {
        "choices": [{"message": {"content": content}}],
        "usage": usage or {"prompt_tokens": 800, "completion_tokens": 40, "total_tokens": 840},
    }


def make_client(handler: Any, **overrides: Any) -> LLMClient:
    """A client whose every request is answered by *handler*."""
    return LLMClient(settings(**overrides), transport=httpx.MockTransport(handler))


def call(client: LLMClient) -> Any:
    """The one call shape this service makes."""
    return client.complete_json(system="s", user="u", schema_name="test", schema=SCHEMA)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_the_provider_name_selects_the_endpoint(self) -> None:
        assert LLMClient(settings())._base_url == PROVIDER_BASE_URLS["gemini"]

    def test_an_explicit_base_url_overrides_the_provider(self) -> None:
        chosen = LLMClient(settings(LLM_BASE_URL="https://example.test/v1/"))
        assert chosen._base_url == "https://example.test/v1"

    def test_configured_needs_both_a_key_and_an_endpoint(self) -> None:
        assert LLMClient(settings()).configured
        assert not LLMClient(settings(LLM_API_KEY="  ")).configured
        assert not LLMClient(settings(LLM_PROVIDER="nowhere")).configured

    def test_the_model_is_the_one_the_call_is_priced_against(self) -> None:
        assert LLMClient(settings(LLM_MODEL="gpt-4o-mini")).model == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


class TestRequest:
    def test_a_valid_response_parses_prices_and_returns(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/chat/completions")
            assert request.headers["Authorization"] == f"Bearer {KEY}"
            body = json.loads(request.content)
            assert body["response_format"]["json_schema"]["strict"] is True
            assert body["temperature"] == 0
            return httpx.Response(200, json=completion('{"intent": "Q1"}'))

        result = call(make_client(handler))
        assert result.data == {"intent": "Q1"}
        assert result.usage.prompt_tokens == 800
        assert result.cost_usd > 0

    def test_the_schema_travels_by_name(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert json.loads(request.content)["response_format"]["json_schema"]["name"] == "test"
            return httpx.Response(200, json=completion("{}"))

        call(make_client(handler))

    def test_an_absent_key_never_reaches_the_network(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("the client must not send a keyless request")

        with pytest.raises(LLMError) as caught:
            call(make_client(handler, LLM_API_KEY="   "))
        assert caught.value.reason == "no_provider_key"

    def test_an_unknown_provider_has_no_endpoint_to_call(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("there is nowhere to send this")

        with pytest.raises(LLMError) as caught:
            call(make_client(handler, LLM_PROVIDER="nowhere"))
        assert caught.value.reason == "provider_error"


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


class TestFailures:
    def test_a_timeout_is_reported_as_one(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        with pytest.raises(LLMError) as caught:
            call(make_client(handler))
        assert caught.value.reason == "provider_timeout"

    def test_a_transport_error_is_a_provider_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        with pytest.raises(LLMError) as caught:
            call(make_client(handler))
        assert caught.value.reason == "provider_error"

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
    def test_a_non_retryable_status_fails_on_the_first_attempt(self, status: int) -> None:
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(status)
            return httpx.Response(status, json={"error": {"message": f"key {KEY} rejected"}})

        with pytest.raises(LLMError) as caught:
            call(make_client(handler))
        assert caught.value.reason == "provider_error"
        assert len(seen) == 1, "a request this code got wrong must not be sent twice"

    def test_the_error_body_never_travels(self) -> None:
        """A provider error echo can contain the key, so only the status is carried."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": {"message": f"bad key {KEY}"}})

        with pytest.raises(LLMError) as caught:
            call(make_client(handler))
        assert KEY not in caught.value.detail
        assert KEY not in str(caught.value)

    @pytest.mark.parametrize("status", [429, 503])
    def test_a_rate_limit_is_retried_then_succeeds(
        self, status: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module.time, "sleep", lambda _: None)
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(status)
            if len(attempts) == 1:
                return httpx.Response(status)
            return httpx.Response(200, json=completion('{"intent": "Q2"}'))

        assert call(make_client(handler)).data == {"intent": "Q2"}
        assert len(attempts) == 2

    def test_a_persistent_rate_limit_gives_up_after_the_last_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module.time, "sleep", lambda _: None)
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(429)
            return httpx.Response(429)

        with pytest.raises(LLMError) as caught:
            call(make_client(handler))
        assert caught.value.reason == "provider_rate_limited"
        assert len(attempts) == MAX_ATTEMPTS

    def test_a_body_that_is_not_json_is_a_provider_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>gateway</html>")

        with pytest.raises(LLMError) as caught:
            call(make_client(handler))
        assert caught.value.reason == "provider_error"

    @pytest.mark.parametrize(
        "body",
        [
            {"choices": []},
            {"choices": [{}]},
            {"usage": {}},
            {"choices": [{"message": {}}]},
        ],
    )
    def test_a_response_carrying_no_content_is_a_provider_error(self, body: dict) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        with pytest.raises(LLMError) as caught:
            call(make_client(handler))
        assert caught.value.reason == "provider_error"

    def test_model_output_that_is_not_json_is_a_provider_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=completion("I'm sorry, I can't do that."))

        with pytest.raises(LLMError) as caught:
            call(make_client(handler))
        assert caught.value.reason == "provider_error"

    def test_model_output_that_is_not_an_object_is_a_provider_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=completion("[1, 2, 3]"))

        with pytest.raises(LLMError) as caught:
            call(make_client(handler))
        assert caught.value.reason == "provider_error"


# ---------------------------------------------------------------------------
# Usage and cost
# ---------------------------------------------------------------------------


class TestUsage:
    def test_a_missing_usage_block_prices_to_zero_rather_than_raising(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

        result = call(make_client(handler))
        assert result.usage.total_tokens == 0
        assert result.cost_usd == 0.0

    def test_the_call_is_priced_against_the_model_that_was_asked_for(self) -> None:
        """A provider that silently serves another model does not pick the rate card."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={**completion("{}"), "model": "gemini-2.5-pro"})

        assert call(make_client(handler)).model == "gemini-2.5-flash"

    def test_closing_releases_the_pool(self) -> None:
        instance = make_client(lambda request: httpx.Response(200, json=completion("{}")))
        instance.close()
        assert instance._client.is_closed
