"""Tests for the structured logging pipeline."""

import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from paimon.config import ObservabilitySettings
from paimon.observability import (
    bind_correlation_id,
    clear_log_context,
    configure_logging,
    get_logger,
)


@pytest.fixture(autouse=True)
def _isolate_logging_state() -> Iterator[None]:
    """Leave global logging state as it was found."""
    clear_log_context()
    yield
    clear_log_context()
    structlog.reset_defaults()
    logging.getLogger().handlers = []


def emitted(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    captured = capsys.readouterr().out.strip()
    return [json.loads(line) for line in captured.splitlines() if line]


class TestJsonOutput:
    def test_a_record_is_a_json_object_with_level_and_timestamp(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(ObservabilitySettings(log_format="json"))
        get_logger("test.json").info("document_ingested", document_id="doc-1")

        (record,) = emitted(capsys)
        assert record["event"] == "document_ingested"
        assert record["document_id"] == "doc-1"
        assert record["level"] == "info"
        assert str(record["timestamp"]).endswith("Z")

    def test_the_service_name_is_attached(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(ObservabilitySettings(service_name="paimon-worker"))
        get_logger("test.service").info("started")

        (record,) = emitted(capsys)
        assert record["service"] == "paimon-worker"

    def test_the_level_threshold_is_applied(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(ObservabilitySettings(log_level="WARNING"))
        logger = get_logger("test.level")
        logger.info("not emitted")
        logger.warning("emitted")

        (record,) = emitted(capsys)
        assert record["event"] == "emitted"


class TestCorrelationId:
    def test_a_generated_id_is_attached_to_every_record(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(ObservabilitySettings())
        correlation_id = bind_correlation_id()
        logger = get_logger("test.correlation")
        logger.info("first")
        logger.info("second")

        records = emitted(capsys)
        assert [record["correlation_id"] for record in records] == [correlation_id] * 2

    def test_an_inbound_id_is_preserved(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A caller's id must survive, so a trace can be followed across services."""
        configure_logging(ObservabilitySettings())
        bind_correlation_id("from-the-caller")
        get_logger("test.inbound").info("handled")

        (record,) = emitted(capsys)
        assert record["correlation_id"] == "from-the-caller"

    def test_clearing_the_context_removes_it(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A recycled worker task must not inherit the previous request's context."""
        configure_logging(ObservabilitySettings())
        bind_correlation_id("first-request")
        clear_log_context()
        get_logger("test.cleared").info("second_request")

        (record,) = emitted(capsys)
        assert "correlation_id" not in record


class TestLibraryLogs:
    def test_standard_library_records_use_the_same_format(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Half the evidence is unqueryable if uvicorn logs prose while we log JSON."""
        configure_logging(ObservabilitySettings())
        bind_correlation_id("shared-id")
        logging.getLogger("uvicorn.error").warning("connection refused")

        (record,) = emitted(capsys)
        assert record["event"] == "connection refused"
        assert record["logger"] == "uvicorn.error"
        assert record["correlation_id"] == "shared-id"

    def test_exceptions_are_rendered_with_a_traceback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(ObservabilitySettings())
        try:
            raise RuntimeError("ingestion failed")
        except RuntimeError:
            get_logger("test.exception").exception("ingestion_error")

        (record,) = emitted(capsys)
        assert "RuntimeError: ingestion failed" in str(record["exception"])
