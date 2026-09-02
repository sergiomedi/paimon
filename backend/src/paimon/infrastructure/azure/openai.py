"""Azure OpenAI adapters.

Azure OpenAI speaks the same request and response shapes as OpenAI; what differs
is how a request is addressed and authenticated. The URL names a *deployment*
rather than a model — a deployment can be called anything, so the deployment name
is configuration and cannot be inferred — and it carries an explicit
``api-version``.

So these adapters own the addressing and the authentication and reuse the
parsing. Writing a second full implementation would have duplicated the part most
likely to go wrong, to avoid duplicating the part least likely to.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from paimon.domain.errors import EmbeddingError, GenerationError
from paimon.domain.ports import Completion, Message
from paimon.domain.value_objects import Embedding
from paimon.infrastructure.azure.credentials import AzureCredential
from paimon.infrastructure.embedding._responses import parse_embeddings

COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"
DEFAULT_API_VERSION = "2024-10-21"
DEFAULT_BATCH_SIZE = 96
DEFAULT_EMBEDDING_TIMEOUT_SECONDS = 30.0
DEFAULT_CHAT_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class AzureOpenAIConfig:
    """Where an Azure OpenAI deployment lives and how to talk to it.

    Attributes:
        endpoint: Resource endpoint, for example
            ``https://my-resource.openai.azure.com``.
        deployment: The deployment name. Not the model name: Azure lets a
            deployment be called anything, and the URL uses this.
        api_version: The API version to request. Pinned rather than defaulted by
            the service, because a version change alters response shapes.
        dimensions: Width to request, for models that support truncation.
        document_prefix: Prepended to text being indexed.
        query_prefix: Prepended to text being searched with.
        batch_size: Texts per embeddings request.
        timeout_seconds: Per-request timeout.
    """

    endpoint: str
    deployment: str
    api_version: str = DEFAULT_API_VERSION
    dimensions: int = 1024
    document_prefix: str = ""
    query_prefix: str = ""
    batch_size: int = DEFAULT_BATCH_SIZE
    timeout_seconds: float = DEFAULT_EMBEDDING_TIMEOUT_SECONDS

    @property
    def base_url(self) -> str:
        """Root URL of the deployment."""
        return f"{self.endpoint.rstrip('/')}/openai/deployments/{self.deployment}"


class _Transport:
    """The half of an Azure OpenAI adapter that is not about embeddings or chat.

    Both adapters address a deployment, authenticate per request and map Azure's
    failures onto domain errors in the same way. Sharing it keeps that identical
    rather than merely similar.
    """

    def __init__(
        self, config: AzureOpenAIConfig, credential: AzureCredential, client: httpx.AsyncClient
    ) -> None:
        self._config = config
        self._credential = credential
        self._client = client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post(
        self, operation: str, payload: dict[str, Any], error_type: type[Exception]
    ) -> Any:
        """Post to an operation on the deployment, mapping failures to a domain error."""
        path = f"/{operation}?api-version={self._config.api_version}"
        label = operation.split("/", maxsplit=1)[0]
        try:
            headers = await self._credential.headers()
            response = await self._client.post(path, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as error:
            detail = _azure_error_detail(error.response)
            msg = f"azure openai {label} returned {error.response.status_code}{detail}"
            raise error_type(msg) from error
        except httpx.HTTPError as error:
            msg = f"azure openai {label} unreachable: {error}"
            raise error_type(msg) from error
        except ValueError as error:
            msg = f"azure openai {label} returned a malformed body: {error}"
            raise error_type(msg) from error


class AzureOpenAIEmbeddingModel:
    """Embeddings from an Azure OpenAI deployment."""

    def __init__(
        self,
        config: AzureOpenAIConfig,
        credential: AzureCredential,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialise the adapter.

        Args:
            config: Endpoint, deployment and request settings.
            credential: How to authenticate. Headers are fetched per request, so
                an Entra token that expires mid-process is refreshed rather than
                failing as a permissions error.
            client: An existing client to use, mainly for tests.
        """
        self._config = config
        self._transport = _Transport(
            config,
            credential,
            client or httpx.AsyncClient(base_url=config.base_url, timeout=config.timeout_seconds),
        )

    @property
    def model_id(self) -> str:
        """The deployment, which is what identifies the vectors this produces."""
        return self._config.deployment

    @property
    def dimensions(self) -> int:
        """Dimensionality of the vectors this model produces."""
        return self._config.dimensions

    async def aclose(self) -> None:
        """Release the underlying connection pool."""
        await self._transport.aclose()

    async def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        """Embed text destined for the index."""
        if not texts:
            return []
        prefixed = [f"{self._config.document_prefix}{text}" for text in texts]
        embeddings: list[Embedding] = []
        for start in range(0, len(prefixed), self._config.batch_size):
            batch = prefixed[start : start + self._config.batch_size]
            embeddings.extend(await self._request(batch))
        return embeddings

    async def embed_query(self, text: str) -> Embedding:
        """Embed a search query."""
        (embedding,) = await self._request([f"{self._config.query_prefix}{text}"])
        return embedding

    async def _request(self, inputs: list[str]) -> list[Embedding]:
        payload = {"input": inputs, "dimensions": self._config.dimensions}
        body = await self._transport.post("embeddings", payload, EmbeddingError)
        return parse_embeddings(
            body,
            expected=len(inputs),
            dimensions=self._config.dimensions,
            model_id=self._config.deployment,
        )


class AzureOpenAIChatModel:
    """Generation from an Azure OpenAI deployment."""

    def __init__(
        self,
        config: AzureOpenAIConfig,
        credential: AzureCredential,
        client: httpx.AsyncClient | None = None,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> None:
        """Initialise the adapter."""
        self._config = config
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._transport = _Transport(
            config,
            credential,
            client or httpx.AsyncClient(base_url=config.base_url, timeout=config.timeout_seconds),
        )

    @property
    def model_id(self) -> str:
        """The deployment that answers."""
        return self._config.deployment

    async def aclose(self) -> None:
        """Release the underlying connection pool."""
        await self._transport.aclose()

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> Completion:
        """Generate an answer."""
        payload: dict[str, Any] = {
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "temperature": self._temperature if temperature is None else temperature,
        }
        limit = max_output_tokens or self._max_output_tokens
        if limit is not None:
            payload["max_tokens"] = limit

        body = await self._transport.post("chat/completions", payload, GenerationError)

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            msg = "chat response contains no message content"
            raise GenerationError(msg) from error

        if not isinstance(text, str) or not text.strip():
            # Azure returns an empty message when a content filter fires, so the
            # reason is surfaced when it is there rather than reported as a
            # generic empty answer.
            reason = _filter_reason(body)
            msg = f"chat provider returned an empty answer{reason}"
            raise GenerationError(msg)

        usage = body.get("usage") or {}
        return Completion(
            text=text,
            model_id=self._config.deployment,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )


def _filter_reason(body: Any) -> str:
    """Describe why a response was empty, when Azure says."""
    try:
        finish = body["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return ""
    return f" (finish_reason={finish})" if finish else ""


def _azure_error_detail(response: httpx.Response) -> str:
    """Extract Azure's own error code, which says far more than the status does.

    A 429 from a quota exhaustion and a 429 from a rate limit want different
    responses from an operator, and only the body distinguishes them.
    """
    try:
        code = response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return ""
    return f" ({code})"
