"""Generation from any endpoint that speaks the OpenAI chat completions API."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from paimon.domain.errors import GenerationError
from paimon.domain.ports import Completion, Message

DEFAULT_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class OpenAICompatibleChatConfig:
    """Everything the adapter needs to reach one endpoint.

    Attributes:
        base_url: Root of the API, for example ``http://localhost:11434/v1``.
        model: Model name the endpoint expects.
        api_key: Bearer token, when the endpoint wants one.
        temperature: Sampling temperature. Zero by default: grounded answering
            wants the same answer for the same sources, and an evaluation run
            over a sampled model measures the sampler as much as the retrieval.
        max_output_tokens: Upper bound on the answer length, if any.
        timeout_seconds: Per-request timeout. Generous, because generation is
            slow and a timeout here costs the whole request.
    """

    base_url: str
    model: str
    api_key: str | None = None
    temperature: float = 0.0
    max_output_tokens: int | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


class OpenAICompatibleChatModel:
    """Calls an OpenAI-shaped ``/chat/completions`` endpoint."""

    def __init__(
        self, config: OpenAICompatibleChatConfig, client: httpx.AsyncClient | None = None
    ) -> None:
        """Initialise the adapter.

        Args:
            config: The endpoint to call and how to call it.
            client: An existing client to use, mainly for tests.
        """
        self._config = config
        headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers=headers,
            timeout=config.timeout_seconds,
        )

    @property
    def model_id(self) -> str:
        """Identifier of the underlying model."""
        return self._config.model

    async def aclose(self) -> None:
        """Release the underlying connection pool."""
        await self._client.aclose()

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> Completion:
        """Generate an answer.

        Args:
            messages: Conversation so far, oldest first.
            temperature: Overrides the configured temperature.
            max_output_tokens: Overrides the configured limit.

        Returns:
            The generated answer and its token usage.

        Raises:
            GenerationError: If the provider failed, or returned something that
                is not a usable answer.
        """
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "temperature": (self._config.temperature if temperature is None else temperature),
        }
        limit = max_output_tokens or self._config.max_output_tokens
        if limit is not None:
            payload["max_tokens"] = limit

        try:
            response = await self._client.post("/chat/completions", json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as error:
            msg = f"chat provider returned {error.response.status_code}"
            raise GenerationError(msg) from error
        except httpx.HTTPError as error:
            msg = f"chat provider unreachable: {error}"
            raise GenerationError(msg) from error
        except ValueError as error:
            msg = f"chat provider returned a malformed body: {error}"
            raise GenerationError(msg) from error

        return self._parse(body)

    def _parse(self, body: Any) -> Completion:
        try:
            choices = body["choices"]
            text = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            msg = "chat response contains no message content"
            raise GenerationError(msg) from error

        if not isinstance(text, str) or not text.strip():
            msg = "chat provider returned an empty answer"
            raise GenerationError(msg)

        # Usage is part of the port's contract, but some OpenAI-compatible
        # servers omit it. Zeros are reported rather than an exception: losing
        # cost attribution for a request is worse than losing the request, and
        # the zeros are visible in the traces that consume them.
        usage = body.get("usage") or {}
        return Completion(
            text=text,
            model_id=str(body.get("model") or self._config.model),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )
