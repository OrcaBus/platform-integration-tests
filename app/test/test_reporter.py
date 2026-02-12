"""
Tests for reporter.py lambda function.
"""

# Import base_test_case first to ensure environment variables are set
from test.base_test_case import LambdaTestCase

import json
import logging
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from lambdas import reporter
from utils.utils import get_safe_timestamp_filename

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class ReporterTests(LambdaTestCase):
    """Test cases for reporter lambda."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        super().setUp()
        self.test_instrument_run_id = "260122_A00001_1234_TEST123456"

    def tearDown(self) -> None:
        """Clean up after tests."""
        super().tearDown()

    def test_handler_raises_when_verify_result_missing_run_id_for_single_service(self):
        """Test that handler raises when verifyResult is missing runId for single service."""
        event = {
            "serviceName": "sequencerunmanager",
            "verifyResult": {"runStatus": "passed"},  # no runId
        }

        with self.assertRaises(ValueError) as context:
            reporter.handler(event, None)

        self.assertIn("runId", str(context.exception))

    def test_handler_accepts_nested_verify_result_for_single_service(self):
        """Test handler accepts verifyResult keyed by service name."""
        run_meta = {
            "testId": f"run#{self.test_instrument_run_id}",
            "sk": "run#meta",
            "serviceName": "sequencerunmanager",
            "startedAt": "2025-01-01T00:00:00Z",
        }

        verify_result = {
            "sequencerunmanager": {
                "runId": self.test_instrument_run_id,
                "runStatus": "passed",
                "matchedEventsCount": 2,
                "missingEventsCount": 0,
                "unexpectedEventsCount": 0,
                "totalExpectedEventsCount": 2,
            }
        }

        template = "<html>{{ testId }}</html>"

        with (
            patch("lambdas.reporter.get_run_meta", return_value=run_meta),
            patch("lambdas.reporter._get_matched_events", return_value=[]),
            patch("lambdas.reporter._get_missing_events", return_value=[]),
            patch("lambdas.reporter._get_unexpected_events", return_value=[]),
            patch("lambdas.reporter.load_reporter_template", return_value=template),
            patch("lambdas.reporter.store_item_to_s3"),
            patch("lambdas.reporter.update_item_to_dynamodb"),
        ):

            event = {
                "serviceName": "sequencerunmanager",
                "verifyResult": verify_result,
            }
            result = reporter.handler(event, None)

        self.assertIn("key", result)
        self.assertIn("reportUrl", result)
        self.assertIn("sequencerunmanager", result["key"])

    def test_handler_accepts_flat_verify_result_for_single_service(self):
        """Test handler accepts flat verifyResult (fallback when not nested)."""
        run_meta = {
            "testId": f"run#{self.test_instrument_run_id}",
            "sk": "run#meta",
            "serviceName": "sequencerunmanager",
            "startedAt": "2025-01-01T00:00:00Z",
        }

        verify_result = {
            "runId": self.test_instrument_run_id,
            "runStatus": "passed",
            "matchedEventsCount": 2,
        }

        with (
            patch("lambdas.reporter.get_run_meta", return_value=run_meta),
            patch("lambdas.reporter._get_matched_events", return_value=[]),
            patch("lambdas.reporter._get_missing_events", return_value=[]),
            patch("lambdas.reporter._get_unexpected_events", return_value=[]),
            patch("lambdas.reporter.load_reporter_template", return_value="<html/>"),
            patch("lambdas.reporter.store_item_to_s3"),
            patch("lambdas.reporter.update_item_to_dynamodb"),
        ):

            event = {
                "serviceName": "sequencerunmanager",
                "verifyResult": verify_result,
            }
            result = reporter.handler(event, None)

        self.assertIn("key", result)
        self.assertIn("reportUrl", result)

    def test_handler_generates_report(self):
        """Test that handler generates HTML report and returns reportUrl."""
        run_meta = {
            "testId": f"run#{self.test_instrument_run_id}",
            "sk": "run#meta",
            "serviceName": "sequencerunmanager",
            "startedAt": "2025-01-01T00:00:00Z",
            "verifiedAt": "2025-01-01T00:05:00Z",
        }

        verify_result = {
            "sequencerunmanager": {
                "runId": self.test_instrument_run_id,
                "runStatus": "passed",
                "matchedEventsCount": 2,
                "missingEventsCount": 0,
                "unexpectedEventsCount": 0,
                "totalExpectedEventsCount": 2,
            }
        }

        matched_events = [
            {
                "testId": f"run#{self.test_instrument_run_id}",
                "sk": "event#1",
                "detailType": "SequenceRunStateChange",
                "source": "Pipe IcaEventPipeConstru-IntegrationTest",
                "status": "matched",
                "expectedOrder": 0,
                "receivedAt": "2025-01-01T00:01:00Z",
                "verifiedAt": "2025-01-01T00:05:00Z",
                "rawS3Key": "events/test.json",
            }
        ]

        template = """
        <html>
            <body>
                <h1>{{ testId }}</h1>
                <p>Status: {{ runStatus }}</p>
                <p>Matched: {{ matchedCount }}</p>
                {{ matchedEventsTable }}
            </body>
        </html>
        """

        with (
            patch("lambdas.reporter.get_run_meta", return_value=run_meta),
            patch("lambdas.reporter._get_matched_events", return_value=matched_events),
            patch("lambdas.reporter._get_missing_events", return_value=[]),
            patch("lambdas.reporter._get_unexpected_events", return_value=[]),
            patch("lambdas.reporter.load_reporter_template", return_value=template),
            patch("lambdas.reporter.store_item_to_s3") as mock_store,
            patch("lambdas.reporter.update_item_to_dynamodb") as mock_update,
        ):

            event = {
                "serviceName": "sequencerunmanager",
                "verifyResult": verify_result,
            }
            result = reporter.handler(event, None)

        self.assertIn("key", result)
        self.assertIn("bucket", result)
        self.assertIn("reportUrl", result)
        self.assertNotIn("url", result)
        self.assertIn("sequencerunmanager", result["key"])
        self.assertIn(self.test_instrument_run_id, result["key"])
        self.assertTrue(result["key"].endswith(".html"))
        self.assertTrue(result["reportUrl"].startswith("s3://"))

        self.assertEqual(mock_store.call_count, 1)
        self.assertEqual(mock_update.call_count, 1)

    def test_handler_generates_report_for_all_services(self):
        """Test that handler generates report when serviceName is 'all'."""
        run_meta = {
            "testId": f"run#{self.test_instrument_run_id}",
            "sk": "run#meta",
            "serviceName": "sequencerunmanager",
            "startedAt": "2025-01-01T00:00:00Z",
        }

        verify_result = {
            "sequencerunmanager": {
                "runId": self.test_instrument_run_id,
                "runStatus": "passed",
                "matchedEventsCount": 2,
                "missingEventsCount": 0,
                "unexpectedEventsCount": 0,
                "totalExpectedEventsCount": 2,
            }
        }

        with (
            patch("lambdas.reporter.get_run_meta", return_value=run_meta),
            patch("lambdas.reporter._get_matched_events", return_value=[]),
            patch("lambdas.reporter._get_missing_events", return_value=[]),
            patch("lambdas.reporter._get_unexpected_events", return_value=[]),
            patch("lambdas.reporter.load_reporter_template", return_value="<html/>"),
            patch("lambdas.reporter.store_item_to_s3"),
            patch("lambdas.reporter.update_item_to_dynamodb"),
        ):

            event = {
                "serviceName": "all",
                "verifyResult": verify_result,
            }
            result = reporter.handler(event, None)

        self.assertIn("key", result)
        self.assertIn("reportUrl", result)
        self.assertIn("all", result["key"])

    def test_handler_handles_all_event_types(self):
        """Test that handler handles matched, missing, and unexpected events."""
        run_meta = {
            "testId": f"run#{self.test_instrument_run_id}",
            "sk": "run#meta",
            "serviceName": "sequencerunmanager",
            "startedAt": "2025-01-01T00:00:00Z",
        }

        verify_result = {
            "sequencerunmanager": {
                "runId": self.test_instrument_run_id,
                "runStatus": "failed",
                "matchedEventsCount": 1,
                "missingEventsCount": 1,
                "unexpectedEventsCount": 1,
                "totalExpectedEventsCount": 2,
            }
        }

        matched_events = [
            {
                "testId": f"run#{self.test_instrument_run_id}",
                "sk": "event#1",
                "detailType": "Event1",
                "source": "Source1",
                "status": "matched",
                "expectedOrder": 0,
            }
        ]

        missing_events = [
            {
                "testId": f"run#{self.test_instrument_run_id}",
                "sk": "event#001-missing",
                "detailType": "Event2",
                "source": "Source2",
                "status": "missed",
                "expectedOrder": 1,
                "expectedEvent": {"detail-type": "Event2", "source": "Source2"},
            }
        ]

        unexpected_events = [
            {
                "testId": f"run#{self.test_instrument_run_id}",
                "sk": "event#999",
                "detailType": "UnexpectedEvent",
                "source": "UnexpectedSource",
                "status": "unexpected",
            }
        ]

        with (
            patch("lambdas.reporter.get_run_meta", return_value=run_meta),
            patch("lambdas.reporter._get_matched_events", return_value=matched_events),
            patch("lambdas.reporter._get_missing_events", return_value=missing_events),
            patch(
                "lambdas.reporter._get_unexpected_events",
                return_value=unexpected_events,
            ),
            patch("lambdas.reporter.load_reporter_template", return_value="<html/>"),
            patch("lambdas.reporter.store_item_to_s3"),
            patch("lambdas.reporter.update_item_to_dynamodb"),
        ):

            event = {
                "serviceName": "sequencerunmanager",
                "verifyResult": verify_result,
            }
            result = reporter.handler(event, None)

        self.assertIn("key", result)

    def test_format_events_table_matched(self):
        """Test formatting matched events table."""
        events = [
            {
                "detailType": "Event1",
                "source": "Source1",
                "eventId": "event-123",
                "receivedAt": "2025-01-01T00:00:00Z",
                "verifiedAt": "2025-01-01T00:05:00Z",
                "expectedOrder": 0,
                "rawS3Key": "events/test.json",
            }
        ]

        html = reporter._format_events_table(events, "matched", "test-bucket")
        self.assertIn("Event1", html)
        self.assertIn("Source1", html)
        self.assertIn("event-123", html)
        self.assertIn("events-table", html)

    def test_format_events_table_missing(self):
        """Test formatting missing events table."""
        events = [
            {
                "detailType": "Event1",
                "source": "Source1",
                "expectedOrder": 0,
                "expectedEvent": {"detail-type": "Event1", "source": "Source1"},
            }
        ]

        html = reporter._format_events_table(events, "missing", "test-bucket")
        self.assertIn("Event1", html)
        self.assertIn("Source1", html)
        self.assertIn("expected-event", html)

    def test_format_events_table_unexpected(self):
        """Test formatting unexpected events table."""
        events = [
            {
                "detailType": "UnexpectedEvent",
                "source": "UnexpectedSource",
                "eventId": "event-999",
                "receivedAt": "2025-01-01T00:00:00Z",
            }
        ]

        html = reporter._format_events_table(events, "unexpected", "test-bucket")
        self.assertIn("UnexpectedEvent", html)
        self.assertIn("UnexpectedSource", html)

    def test_format_events_table_empty(self):
        """Test formatting empty events table."""
        html_matched = reporter._format_events_table([], "matched", "test-bucket")
        self.assertIn("No matched events", html_matched)

        html_missing = reporter._format_events_table([], "missing", "test-bucket")
        self.assertIn("No missing events", html_missing)

        html_unexpected = reporter._format_events_table([], "unexpected", "test-bucket")
        self.assertIn("No unexpected events", html_unexpected)

    def test_render_template(self):
        """Test template rendering."""
        template = "Hello {{ name }}, you have {{ count }} items."
        context = {"name": "TestUser", "count": 5}

        result = reporter._render_template(template, context)
        self.assertIn("TestUser", result)
        self.assertIn("5", result)
        self.assertNotIn("{{", result)

    def test_render_template_with_dict(self):
        """Test template rendering with dict values."""
        template = "Data: {{ data }}"
        context = {"data": {"key": "value"}}

        result = reporter._render_template(template, context)
        self.assertIn("key", result)
        self.assertIn("value", result)

    def test_safe_timestamp_filename(self):
        """Test timestamp filename generation (utils.get_safe_timestamp_filename)."""
        dt = datetime(2025, 1, 1, 12, 30, 45, tzinfo=timezone.utc)
        filename = get_safe_timestamp_filename(dt)
        self.assertEqual(filename, "2025-01-01T12-30-45Z")
        self.assertNotIn(":", filename)
