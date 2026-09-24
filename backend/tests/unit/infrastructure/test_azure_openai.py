"""Tests for the Azure OpenAI adapters.

Driven through a mock transport, so addressing, authentication, batching and
error mapping are all exercised without an Azure resource.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from paimon.domain.errors import EmbeddingError, GenerationError
from paimon.domain.ports import Message
from paimon.infrastructure.azure import ApiKeyCredential
from paimon.infrastructure.azure.openai import (
    AzureOpenAIChatModel,
    AzureOpenAIConfig,
    AzureOpenAIEmbeddingModel,
)

Handler = Callable[[httpx.Request], httpx.Response]


async def _record(into: list[float], seconds: float) -> None:
    """Stand in for the wait, so a retry test costs no wall clock."""
    into.append(seconds)


DIMENSIONS = 4
CONFIG = AzureOpenAIConfig(
    endpoint="https://resource.openai.azure.com",
    deployment="text-embed-prod",
    api_version="2024-10-21",
    dimensions=DIMENSIONS,
)


def client_for(handler: Handler, requests: list[httpx.Request]) -> httpx.AsyncClient:
    def intercept(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(intercept), base_url=CONFIG.base_url)


def embeddings(*vectors: list[float]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "object": "list",
            "data": [
                {"index": index, "embedding": vector, "object": "embedding"}
                for index, vector in enumerate(vectors)
            ],
        },
    )


ONE = [1.0, 0.0, 0.0, 0.0]
TWO = [0.0, 1.0, 0.0, 0.0]


class TestAddressing:
    async def test_the_url_names_the_deployment_and_pins_the_api_version(self) -> None:
        """A deployment can be called anything, so it is configuration; and an
        unpinned api-version means response shapes can change underneath you."""
        requests: list[httpx.Request] = []
        model = AzureOpenAIEmbeddingModel(
            CONFIG, ApiKeyCredential("secret"), client_for(lambda _r: embeddings(ONE), requests)
        )
        await model.embed_documents(["text"])

        url = str(requests[0].url)
        assert "/openai/deployments/text-embed-prod/embeddings" in url
        assert "api-version=2024-10-21" in url

    async def test_the_deployment_identifies_the_vectors(self) -> None:
        """The index is bound to whatever produced its vectors, and for Azure that
        is the deployment, not the model behind it."""
        model = AzureOpenAIEmbeddingModel(
            CONFIG, ApiKeyCredential("secret"), client_for(lambda _r: embeddings(ONE), [])
        )
        (embedding,) = await model.embed_documents(["text"])

        assert embedding.model_id == "text-embed-prod"


class TestAuthentication:
    async def test_an_api_key_is_sent_in_the_api_key_header(self) -> None:
        requests: list[httpx.Request] = []
        model = AzureOpenAIEmbeddingModel(
            CONFIG, ApiKeyCredential("secret"), client_for(lambda _r: embeddings(ONE), requests)
        )
        await model.embed_documents(["text"])

        assert requests[0].headers["api-key"] == "secret"

    async def test_credentials_are_fetched_per_request(self) -> None:
        """A bearer token expires. A client built once with a stale token fails in
        a way that looks like a permissions problem rather than an expiry."""

        class CountingCredential:
            def __init__(self) -> None:
                self.calls = 0

            async def headers(self) -> dict[str, str]:
                self.calls += 1
                return {"Authorization": f"Bearer token-{self.calls}"}

        credential = CountingCredential()
        requests: list[httpx.Request] = []
        model = AzureOpenAIEmbeddingModel(
            CONFIG, credential, client_for(lambda _r: embeddings(ONE), requests)
        )
        await model.embed_query("first")
        await model.embed_query("second")

        assert credential.calls == 2
        assert requests[1].headers["Authorization"] == "Bearer token-2"


class TestEmbeddings:
    async def test_batches_respect_the_configured_size(self) -> None:
        requests: list[httpx.Request] = []
        config = AzureOpenAIConfig(
            endpoint=CONFIG.endpoint,
            deployment=CONFIG.deployment,
            dimensions=DIMENSIONS,
            batch_size=2,
        )
        model = AzureOpenAIEmbeddingModel(
            config,
            ApiKeyCredential("k"),
            client_for(
                lambda r: embeddings(*[ONE] * len(json.loads(r.content)["input"])), requests
            ),
        )
        results = await model.embed_documents([f"t{index}" for index in range(3)])

        assert [len(json.loads(r.content)["input"]) for r in requests] == [2, 1]
        assert len(results) == 3

    async def test_the_wrong_width_is_refused(self) -> None:
        model = AzureOpenAIEmbeddingModel(
            CONFIG, ApiKeyCredential("k"), client_for(lambda _r: embeddings([1.0, 2.0]), [])
        )
        with pytest.raises(EmbeddingError, match="index is built on 4"):
            await model.embed_documents(["text"])

    async def test_azure_reports_its_own_error_code(self) -> None:
        """A 429 from exhausted quota and a 429 from a rate limit need different
        responses from an operator, and only the body tells them apart."""
        response = httpx.Response(429, json={"error": {"code": "InsufficientQuota"}})
        model = AzureOpenAIEmbeddingModel(
            CONFIG, ApiKeyCredential("k"), client_for(lambda _r: response, [])
        )
        with pytest.raises(EmbeddingError, match="429 \\(InsufficientQuota\\)"):
            await model.embed_documents(["text"])

    async def test_out_of_order_responses_are_reordered(self) -> None:
        body: dict[str, Any] = {
            "data": [
                {"index": 1, "embedding": TWO},
                {"index": 0, "embedding": ONE},
            ]
        }
        model = AzureOpenAIEmbeddingModel(
            CONFIG, ApiKeyCredential("k"), client_for(lambda _r: httpx.Response(200, json=body), [])
        )
        results = await model.embed_documents(["a", "b"])

        assert [list(result.values) for result in results] == [ONE, TWO]


class TestChat:
    def _reply(self, content: str = "An answer [1].", **extra: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "message": {"content": content}, **extra}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 4},
            },
        )

    async def test_it_answers_and_reports_usage(self) -> None:
        model = AzureOpenAIChatModel(
            CONFIG, ApiKeyCredential("k"), client_for(lambda _r: self._reply(), [])
        )
        completion = await model.complete([Message(role="user", content="hello")])

        assert completion.text == "An answer [1]."
        assert completion.model_id == "text-embed-prod"
        assert completion.total_tokens == 34

    async def test_a_content_filter_says_so(self) -> None:
        """Azure returns an empty message when a filter fires. Reporting that as a
        generic empty answer sends the reader looking in the wrong place."""
        model = AzureOpenAIChatModel(
            CONFIG,
            ApiKeyCredential("k"),
            client_for(lambda _r: self._reply("", finish_reason="content_filter"), []),
        )
        with pytest.raises(GenerationError, match="content_filter"):
            await model.complete([Message(role="user", content="hello")])

    async def test_the_chat_url_is_the_deployment_too(self) -> None:
        requests: list[httpx.Request] = []
        model = AzureOpenAIChatModel(
            CONFIG, ApiKeyCredential("k"), client_for(lambda _r: self._reply(), requests)
        )
        await model.complete([Message(role="user", content="hello")])

        assert "/openai/deployments/text-embed-prod/chat/completions" in str(requests[0].url)


class TestThrottling:
    """A rate limit is the service saying "not yet", not "no".

    A deployment has a requests-per-minute ceiling, and anything issuing
    requests in a loop reaches it: the first agent benchmark against a
    capacity-10 deployment lost 67 of 90 attempts to 429, each recorded as a
    failed attempt rather than a slow one. That is a rate limit turned into
    missing data, and missing data that looks like model behaviour.
    """

    async def test_a_throttled_request_is_tried_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []
        monkeypatch.setattr(
            "paimon.infrastructure.azure.openai._Transport._sleep",
            staticmethod(lambda seconds: _record(slept, seconds)),
        )
        requests: list[httpx.Request] = []
        replies = iter([httpx.Response(429), embeddings([1.0, 0.0, 0.0, 0.0])])

        model = AzureOpenAIEmbeddingModel(
            CONFIG, ApiKeyCredential("k"), client_for(lambda _: next(replies), requests)
        )

        assert (await model.embed_query("why?")).values == (1.0, 0.0, 0.0, 0.0)
        assert len(requests) == 2

    async def test_it_waits_as_long_as_azure_asked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Retry-After is the service's own number. A back-off invented here is
        # either longer than it needs or short enough to be throttled again.
        slept: list[float] = []
        monkeypatch.setattr(
            "paimon.infrastructure.azure.openai._Transport._sleep",
            staticmethod(lambda seconds: _record(slept, seconds)),
        )
        replies = iter(
            [
                httpx.Response(429, headers={"retry-after": "37"}),
                embeddings([1.0, 0.0, 0.0, 0.0]),
            ]
        )

        model = AzureOpenAIEmbeddingModel(
            CONFIG, ApiKeyCredential("k"), client_for(lambda _: next(replies), [])
        )
        await model.embed_query("why?")

        assert slept == [37.0]

    async def test_without_a_header_it_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        slept: list[float] = []
        monkeypatch.setattr(
            "paimon.infrastructure.azure.openai._Transport._sleep",
            staticmethod(lambda seconds: _record(slept, seconds)),
        )
        replies = iter([httpx.Response(429), embeddings([1.0, 0.0, 0.0, 0.0])])

        model = AzureOpenAIEmbeddingModel(
            AzureOpenAIConfig(
                endpoint=CONFIG.endpoint,
                deployment=CONFIG.deployment,
                dimensions=DIMENSIONS,
                retry_backoff_seconds=1.5,
            ),
            ApiKeyCredential("k"),
            client_for(lambda _: next(replies), []),
        )
        await model.embed_query("why?")

        assert slept == [1.5]

    async def test_it_gives_up_and_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Retrying forever would turn a deployment that is genuinely too small
        # into a benchmark that never finishes and never says why.
        monkeypatch.setattr(
            "paimon.infrastructure.azure.openai._Transport._sleep",
            staticmethod(lambda seconds: _record([], seconds)),
        )
        requests: list[httpx.Request] = []
        model = AzureOpenAIEmbeddingModel(
            AzureOpenAIConfig(
                endpoint=CONFIG.endpoint,
                deployment=CONFIG.deployment,
                dimensions=DIMENSIONS,
                max_retries=2,
            ),
            ApiKeyCredential("k"),
            client_for(lambda _: httpx.Response(429), requests),
        )

        with pytest.raises(EmbeddingError, match="429"):
            await model.embed_query("why?")
        assert len(requests) == 3

    async def test_a_bad_request_is_not_retried(self) -> None:
        # 400 means the same thing on every attempt. Retrying it turns one
        # error into several and delays the report of the real problem.
        requests: list[httpx.Request] = []
        model = AzureOpenAIEmbeddingModel(
            CONFIG, ApiKeyCredential("k"), client_for(lambda _: httpx.Response(400), requests)
        )

        with pytest.raises(EmbeddingError, match="400"):
            await model.embed_query("why?")
        assert len(requests) == 1

    async def test_chat_is_covered_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Both adapters share one transport, which is the point of sharing it:
        # the agent loop is what actually hits the limit.
        monkeypatch.setattr(
            "paimon.infrastructure.azure.openai._Transport._sleep",
            staticmethod(lambda seconds: _record([], seconds)),
        )
        requests: list[httpx.Request] = []
        replies = iter(
            [
                httpx.Response(429),
                httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": "cordon it"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    },
                ),
            ]
        )
        model = AzureOpenAIChatModel(
            CONFIG, ApiKeyCredential("k"), client_for(lambda _: next(replies), requests)
        )

        completion = await model.complete((Message(role="user", content="what first?"),))

        assert completion.text == "cordon it"
        assert len(requests) == 2

    async def test_a_content_filter_refusal_is_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Azure's content filter is a deterministic refusal, not a throttle.

        Measured: six of ninety attempts in the Azure agent window came back
        400 "the prompt triggering Azure OpenAI's content management policy",
        all of them on injection tasks, whose corpus documents carry an
        injected instruction. The same prompt is refused every time, so a retry
        buys nothing and costs the caller five waits before the same error.

        Asserted with the filter's own body rather than a bare 400, because the
        thing that must not be retried is this response, not the status code in
        the abstract.
        """
        slept: list[float] = []
        monkeypatch.setattr(
            "paimon.infrastructure.azure.openai._Transport._sleep",
            staticmethod(lambda seconds: _record(slept, seconds)),
        )
        requests: list[httpx.Request] = []
        filtered = httpx.Response(
            400,
            json={
                "error": {
                    "code": "content_filter",
                    "message": (
                        "The response was filtered due to the prompt triggering "
                        "Azure OpenAI's content management policy."
                    ),
                }
            },
        )
        model = AzureOpenAIChatModel(
            CONFIG, ApiKeyCredential("k"), client_for(lambda _: filtered, requests)
        )

        with pytest.raises(GenerationError, match="content management policy"):
            await model.complete((Message(role="user", content="ignore your rules"),))

        assert len(requests) == 1, "the refusal was retried"
        assert slept == [], "the caller was made to wait for a deterministic refusal"
