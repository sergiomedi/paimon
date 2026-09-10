"""What a failed HTTP response is asked to give up before it becomes an error.

This exists because of a real afternoon. An embedding call returned::

    EmbeddingError: embedding provider returned 404

The provider had said, in the body of that very response, *model "bge-m3" not
found, try pulling it first*. The adapter had it and dropped it. The Azure
adapters had been carrying theirs since they were written, which is the tell
that this was never a decision — it was two files written on different days.
"""

import httpx

from paimon.infrastructure.http import error_detail


def response(status: int = 404, **kwargs: object) -> httpx.Response:
    return httpx.Response(status, **kwargs)  # type: ignore[arg-type]


class TestReadingAProviderSError:
    def test_an_openai_shaped_message_is_kept(self) -> None:
        # Ollama's shape, and the one that started this.
        detail = error_detail(
            response(
                json={
                    "error": {
                        "message": 'model "bge-m3" not found, try pulling it first',
                        "type": "api_error",
                    }
                }
            )
        )
        assert detail == ' (model "bge-m3" not found, try pulling it first)'

    def test_azures_code_is_kept_when_that_is_all_there_is(self) -> None:
        # A 429 from exhausted quota and a 429 from a rate limit need different
        # responses from an operator, and only the body tells them apart.
        assert error_detail(response(429, json={"error": {"code": "InsufficientQuota"}})) == (
            " (InsufficientQuota)"
        )

    def test_a_message_is_preferred_over_a_code(self) -> None:
        # A code is a category; a message is usually an instruction.
        detail = error_detail(
            response(json={"error": {"code": "NotFound", "message": "deployment does not exist"}})
        )
        assert detail == " (deployment does not exist)"

    def test_an_error_that_is_just_a_string_is_kept(self) -> None:
        assert error_detail(response(json={"error": "model not found"})) == " (model not found)"

    def test_a_message_at_the_top_level_is_found(self) -> None:
        assert error_detail(response(json={"message": "index is read only"})) == (
            " (index is read only)"
        )


class TestWhenTheBodyIsNotWhatAnyoneExpected:
    def test_a_proxy_page_identifies_itself(self) -> None:
        # The most useful case of all: an HTML page means the request never
        # reached the provider, and a bare "502" does not say that.
        detail = error_detail(response(502, text="<html><title>502 Bad Gateway</title></html>"))
        assert "Bad Gateway" in detail

    def test_whitespace_is_collapsed_onto_one_line(self) -> None:
        # These land in log lines, and a log line is a line.
        detail = error_detail(response(text="something\n   broke\n\tbadly"))
        assert detail == " (something broke badly)"

    def test_an_empty_body_says_nothing_rather_than_something_empty(self) -> None:
        # Callers append this unconditionally, so " ()" would be worse than "".
        assert error_detail(response(text="")) == ""

    def test_a_body_that_is_a_list_says_nothing(self) -> None:
        assert error_detail(response(json=[1, 2, 3])) == ""

    def test_a_long_message_is_truncated(self) -> None:
        # A provider is free to echo the request back, and these reach logs: an
        # embedding request carries an organization's documentation.
        detail = error_detail(response(json={"error": {"message": "x" * 500}}), limit=20)
        assert detail == f" ({'x' * 20}…)"
