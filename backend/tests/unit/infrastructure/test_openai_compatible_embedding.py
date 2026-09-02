"""Tests for the OpenAI-compatible embedding adapter.

Driven through a mock transport rather than a live endpoint, so the adapter's
own behaviour — batching, ordering, validation and error mapping — is exercised
without a model server.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from paimon.domain.errors import EmbeddingError
from paimon.infrastructure.embedding import OpenAICompatibleConfig, OpenAICompatibleEmbeddingModel

DIMENSIONS = 4


Handler = Callable[[httpx.Request], httpx.Response]


def build(
    handler: Handler, **overrides: Any
) -> tuple[OpenAICompatibleEmbeddingModel, list[dict[str, Any]]]:
    """An adapter wired to a scripted transport, plus the requests it made."""
    captured: list[dict[str, Any]] = []

    def intercept(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return handler(request)

    config = OpenAICompatibleConfig(
        base_url="http://model.test/v1",
        model="test-embed",
        dimensions=DIMENSIONS,
        **overrides,
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(intercept), base_url="http://model.test/v1"
    )
    return OpenAICompatibleEmbeddingModel(config, client=client), captured


def payload(*vectors: list[float], shuffle: bool = False) -> httpx.Response:
    data = [
        {"index": index, "embedding": vector, "object": "embedding"}
        for index, vector in enumerate(vectors)
    ]
    if shuffle:
        data.reverse()
    return httpx.Response(200, json={"object": "list", "data": data, "model": "test-embed"})


ONE = [1.0, 0.0, 0.0, 0.0]
TWO = [0.0, 1.0, 0.0, 0.0]
THREE = [0.0, 0.0, 1.0, 0.0]


class TestRequests:
    async def test_it_posts_the_model_and_the_dimensions(self) -> None:
        model, captured = build(lambda _request: payload(ONE))
        await model.embed_documents(["text"])

        assert captured[0]["model"] == "test-embed"
        assert captured[0]["dimensions"] == DIMENSIONS

    async def test_prefixes_are_applied_to_the_right_side(self) -> None:
        """Asymmetric models want an instruction on the query and nothing on the
        document; sending the wrong one silently costs retrieval quality."""
        model, captured = build(
            lambda _request: payload(ONE),
            document_prefix="passage: ",
            query_prefix="query: ",
        )
        await model.embed_documents(["a runbook"])
        await model.embed_query("how to drain")

        assert captured[0]["input"] == ["passage: a runbook"]
        assert captured[1]["input"] == ["query: how to drain"]

    async def test_large_inputs_are_split_into_batches(self) -> None:
        """Providers cap the number of inputs per call."""
        model, captured = build(
            lambda request: payload(*[ONE] * len(json.loads(request.content)["input"])),
            batch_size=2,
        )
        results = await model.embed_documents([f"text {index}" for index in range(5)])

        assert [len(call["input"]) for call in captured] == [2, 2, 1]
        assert len(results) == 5

    async def test_an_empty_input_makes_no_request(self) -> None:
        """A document that chunks to nothing must not become a provider call."""
        model, captured = build(lambda _request: payload())
        assert await model.embed_documents([]) == []
        assert captured == []


class TestResponses:
    async def test_embeddings_are_returned_in_input_order(self) -> None:
        """Results are matched to inputs by position, so a provider answering out
        of order would attach every embedding to the wrong chunk."""
        model, _ = build(lambda _request: payload(ONE, TWO, THREE, shuffle=True))
        results = await model.embed_documents(["a", "b", "c"])

        assert [list(result.values) for result in results] == [ONE, TWO, THREE]

    async def test_every_embedding_records_the_model(self) -> None:
        model, _ = build(lambda _request: payload(ONE))
        (result,) = await model.embed_documents(["text"])

        assert result.model_id == "test-embed"


class TestFailures:
    async def test_an_http_error_becomes_an_embedding_error(self) -> None:
        model, _ = build(lambda _request: httpx.Response(500, json={"error": "boom"}))
        with pytest.raises(EmbeddingError, match="returned 500"):
            await model.embed_documents(["text"])

    async def test_an_unreachable_provider_becomes_an_embedding_error(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        model, _ = build(refuse)
        with pytest.raises(EmbeddingError, match="unreachable"):
            await model.embed_documents(["text"])

    async def test_a_body_without_data_is_refused(self) -> None:
        model, _ = build(lambda _request: httpx.Response(200, json={"object": "list"}))
        with pytest.raises(EmbeddingError, match="no 'data' array"):
            await model.embed_documents(["text"])

    async def test_a_short_response_is_refused(self) -> None:
        """Silently returning fewer embeddings than inputs would misalign every
        chunk after the gap."""
        model, _ = build(lambda _request: payload(ONE))
        with pytest.raises(EmbeddingError, match="asked for 2 embeddings"):
            await model.embed_documents(["a", "b"])

    async def test_the_wrong_dimensionality_is_refused(self) -> None:
        """The index is built on a fixed size; a vector of another size cannot go
        into it, and finding out at write time beats finding out at query time."""
        model, _ = build(lambda _request: payload([1.0, 2.0]))
        with pytest.raises(EmbeddingError, match="but the index is built on 4"):
            await model.embed_documents(["text"])

    async def test_a_malformed_entry_is_refused(self) -> None:
        model, _ = build(
            lambda _request: httpx.Response(200, json={"data": [{"index": 0, "embedding": "nope"}]})
        )
        with pytest.raises(EmbeddingError, match="malformed"):
            await model.embed_documents(["text"])
