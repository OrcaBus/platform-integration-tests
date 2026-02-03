"""
Tests for verifier.py lambda function.
"""

# Import base_test_case first to ensure environment variables are set
from test.base_test_case import LambdaTestCase

import json
import logging
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

from lambdas import verifier

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class VerifierTests(LambdaTestCase):
    """Test cases for verifier lambda."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        super().setUp()
        self.test_instrument_run_id = "260122_A00001_1234_TEST123456"

    def tearDown(self) -> None:
        """Clean up after tests."""
        super().tearDown()

    def test_handler_requires_test_instrument_run_id(self):
        """Test that handler raises error when test instrument run ID is missing."""
        event = {"mode": "status"}

        with self.assertRaises(ValueError) as context:
            verifier.handler(event, None)

        self.assertIn("testInstrumentRunId", str(context.exception))

    def test_handler_status_mode_returns_unknown_when_no_meta(self):
        """Test status mode returns unknown when run meta doesn't exist."""
        event = {
            "testInstrumentRunId": self.test_instrument_run_id,
            "mode": "status",
        }

        with patch("lambdas.verifier._get_run_meta", return_value=None):
            result = verifier.handler(event, None)

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["runId"], self.test_instrument_run_id)

    def test_handler_status_mode_returns_running(self):
        """Test status mode returns running when not all events observed."""
        run_meta = {
            "testId": f"run#{self.test_instrument_run_id}",
            "sk": "run#meta",
            "status": "running",
            "serviceName": "all",
            "timeoutAt": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
        }

        expectations = [
            {"detail-type": "Event1", "source": "Source1"},
            {"detail-type": "Event2", "source": "Source2"},
        ]

        observed_events = [
            {"testId": f"run#{self.test_instrument_run_id}", "sk": "event#1"},
        ]

        with (
            patch("lambdas.verifier._get_run_meta", return_value=run_meta),
            patch(
                "lambdas.verifier.get_item_from_s3",
                return_value=json.dumps(expectations),
            ),
            patch(
                "lambdas.verifier.get_items_from_dynamodb", return_value=observed_events
            ),
            patch("lambdas.verifier.update_item_in_dynamodb"),
        ):

            event = {
                "testInstrumentRunId": self.test_instrument_run_id,
                "mode": "status",
            }
            result = verifier.handler(event, None)

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["observedCount"], 1)
        self.assertEqual(result["expectedCount"], 2)

    def test_handler_status_mode_returns_ready(self):
        """Test status mode returns ready when all events observed."""
        run_meta = {
            "testId": f"run#{self.test_instrument_run_id}",
            "sk": "run#meta",
            "status": "running",
            "serviceName": "all",
            "timeoutAt": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
        }

        expectations = [
            {"detail-type": "Event1", "source": "Source1"},
            {"detail-type": "Event2", "source": "Source2"},
        ]

        observed_events = [
            {"testId": f"run#{self.test_instrument_run_id}", "sk": "event#1"},
            {"testId": f"run#{self.test_instrument_run_id}", "sk": "event#2"},
        ]

        with (
            patch("lambdas.verifier._get_run_meta", return_value=run_meta),
            patch(
                "lambdas.verifier.get_item_from_s3",
                return_value=json.dumps(expectations),
            ),
            patch(
                "lambdas.verifier.get_items_from_dynamodb", return_value=observed_events
            ),
            patch("lambdas.verifier.update_item_in_dynamodb"),
        ):

            event = {
                "testInstrumentRunId": self.test_instrument_run_id,
                "mode": "status",
            }
            result = verifier.handler(event, None)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["observedCount"], 2)
        self.assertEqual(result["expectedCount"], 2)

    def test_handler_status_mode_returns_timeout(self):
        """Test status mode returns timeout when timeout threshold is reached."""
        timeout_at = datetime.now(timezone.utc) - timedelta(
            minutes=1
        )  # Already timed out
        run_meta = {
            "testId": f"run#{self.test_instrument_run_id}",
            "sk": "run#meta",
            "status": "running",
            "serviceName": "all",
            "timeoutAt": timeout_at.isoformat(),
        }

        expectations = [
            {"detail-type": "Event1", "source": "Source1"},
        ]

        observed_events = [
            {"testId": f"run#{self.test_instrument_run_id}", "sk": "event#1"},
        ]

        with (
            patch("lambdas.verifier._get_run_meta", return_value=run_meta),
            patch(
                "lambdas.verifier.get_item_from_s3",
                return_value=json.dumps(expectations),
            ),
            patch(
                "lambdas.verifier.get_items_from_dynamodb", return_value=observed_events
            ),
            patch("lambdas.verifier.update_item_in_dynamodb"),
        ):

            event = {
                "testInstrumentRunId": self.test_instrument_run_id,
                "mode": "status",
            }
            result = verifier.handler(event, None)

        self.assertEqual(result["status"], "timeout")

    def test_handler_verify_mode_matches_events(self):
        """Test verify mode matches expected events."""
        run_meta = {
            "testId": f"run#{self.test_instrument_run_id}",
            "sk": "run#meta",
            "status": "ready",
            "serviceName": "all",
        }

        expectations = [
            {
                "detail-type": "SequenceRunStateChange",
                "source": "Pipe IcaEventPipeConstru-IntegrationTest",
                "__match": {"fields": ["detail.instrumentRunId"]},
                "__replace": {"testInstrumentRunIdField": ["detail.instrumentRunId"]},
            }
        ]

        # Mock observed event that matches
        observed_event_meta = {
            "testId": f"run#{self.test_instrument_run_id}",
            "sk": "event#123",
            "detailType": "SequenceRunStateChange",
            "source": "Pipe IcaEventPipeConstru-IntegrationTest",
            "rawS3Key": "events/test.json",
        }

        observed_event_body = {
            "detail-type": "SequenceRunStateChange",
            "source": "Pipe IcaEventPipeConstru-IntegrationTest",
            "detail": {"instrumentRunId": self.test_instrument_run_id},
        }

        def s3_side_effect(key):
            if "expectations.json" in key:
                return json.dumps(expectations)
            return json.dumps(observed_event_body)

        with (
            patch("lambdas.verifier._get_run_meta", return_value=run_meta),
            patch("lambdas.verifier.get_item_from_s3", side_effect=s3_side_effect),
            patch(
                "lambdas.verifier._get_observed_events",
                return_value=[observed_event_meta],
            ),
            patch("lambdas.verifier.update_item_in_dynamodb") as mock_update,
            patch("lambdas.verifier.get_items_from_dynamodb", return_value=[]),
        ):

            event = {
                "testInstrumentRunId": self.test_instrument_run_id,
                "mode": "verify",
            }
            result = verifier.handler(event, None)

        self.assertEqual(result["matchedCount"], 1)
        self.assertEqual(result["missingCount"], 0)
        self.assertEqual(result["unexpectedCount"], 0)
        self.assertEqual(result["runStatus"], "passed")

    def test_handler_verify_mode_detects_missing_events(self):
        """Test verify mode detects missing events."""
        run_meta = {
            "testId": f"run#{self.test_instrument_run_id}",
            "sk": "run#meta",
            "status": "ready",
            "serviceName": "all",
        }

        expectations = [
            {
                "detail-type": "SequenceRunStateChange",
                "source": "Pipe IcaEventPipeConstru-IntegrationTest",
                "__match": {"fields": ["detail.instrumentRunId"]},
            }
        ]

        # No observed events
        with (
            patch("lambdas.verifier._get_run_meta", return_value=run_meta),
            patch(
                "lambdas.verifier.get_item_from_s3",
                return_value=json.dumps(expectations),
            ),
            patch("lambdas.verifier._get_observed_events", return_value=[]),
            patch("lambdas.verifier.put_item_to_dynamodb") as mock_put,
            patch("lambdas.verifier.update_item_in_dynamodb") as mock_update,
            patch("lambdas.verifier.get_items_from_dynamodb", return_value=[]),
        ):

            event = {
                "testInstrumentRunId": self.test_instrument_run_id,
                "mode": "verify",
            }
            result = verifier.handler(event, None)

        self.assertEqual(result["matchedCount"], 0)
        self.assertEqual(result["missingCount"], 1)
        self.assertEqual(result["runStatus"], "failed")

        # Verify missing event was written
        self.assertEqual(mock_put.call_count, 1)
        put_call = mock_put.call_args[0][0]
        self.assertEqual(put_call["status"], "missed")
        self.assertTrue(put_call["sk"].startswith("expectation#"))

    def test_handler_verify_mode_detects_unexpected_events(self):
        """Test verify mode detects unexpected events."""
        run_meta = {
            "testId": f"run#{self.test_instrument_run_id}",
            "sk": "run#meta",
            "status": "ready",
            "serviceName": "all",
        }

        expectations = [
            {
                "detail-type": "SequenceRunStateChange",
                "source": "Pipe IcaEventPipeConstru-IntegrationTest",
                "__match": {"fields": ["detail.instrumentRunId"]},
            }
        ]

        # Observed event that doesn't match
        observed_event_meta = {
            "testId": f"run#{self.test_instrument_run_id}",
            "sk": "event#123",
            "detailType": "UnexpectedEvent",
            "source": "DifferentSource",
            "rawS3Key": "events/test.json",
        }

        with (
            patch("lambdas.verifier._get_run_meta", return_value=run_meta),
            patch(
                "lambdas.verifier.get_item_from_s3",
                return_value=json.dumps(expectations),
            ),
            patch("lambdas.verifier._get_observed_events", return_value=[]),
            patch("lambdas.verifier.put_item_to_dynamodb"),
            patch("lambdas.verifier.update_item_in_dynamodb") as mock_update,
            patch(
                "lambdas.verifier.get_items_from_dynamodb",
                return_value=[observed_event_meta],
            ),
        ):

            event = {
                "testInstrumentRunId": self.test_instrument_run_id,
                "mode": "verify",
            }
            result = verifier.handler(event, None)

        self.assertEqual(result["matchedCount"], 0)
        self.assertEqual(result["missingCount"], 1)
        self.assertEqual(result["unexpectedCount"], 1)
        self.assertEqual(result["runStatus"], "failed")

    def test_handler_accepts_different_run_id_field_names(self):
        """Test handler accepts different field names for run ID."""
        run_meta = {
            "testId": f"run#{self.test_instrument_run_id}",
            "sk": "run#meta",
            "status": "running",
            "serviceName": "all",
        }

        # Test with testRunId
        with (
            patch("lambdas.verifier._get_run_meta", return_value=run_meta),
            patch("lambdas.verifier.get_item_from_s3", return_value=json.dumps([])),
            patch("lambdas.verifier.get_items_from_dynamodb", return_value=[]),
        ):

            event = {
                "testRunId": self.test_instrument_run_id,
                "mode": "status",
            }
            result = verifier.handler(event, None)
            self.assertEqual(result["runId"], self.test_instrument_run_id)

        # Test with runId
        with (
            patch("lambdas.verifier._get_run_meta", return_value=run_meta),
            patch("lambdas.verifier.get_item_from_s3", return_value=json.dumps([])),
            patch("lambdas.verifier.get_items_from_dynamodb", return_value=[]),
        ):

            event = {
                "runId": self.test_instrument_run_id,
                "mode": "status",
            }
            result = verifier.handler(event, None)
            self.assertEqual(result["runId"], self.test_instrument_run_id)

    def test_match_event(self):
        """Test event matching logic."""
        expected = {
            "detail": {
                "instrumentRunId": "260122_A00001_1234_TEST123456",
                "status": "Complete",
            }
        }
        observed = {
            "detail": {
                "instrumentRunId": "260122_A00001_1234_TEST123456",
                "status": "Complete",
            }
        }
        match_fields = ["detail.instrumentRunId", "detail.status"]

        result = verifier._match_event(expected, observed, match_fields)
        self.assertTrue(result)

        # Test mismatch
        observed2 = {
            "detail": {
                "instrumentRunId": "260122_A00001_1234_TEST123456",
                "status": "Running",
            }
        }
        result2 = verifier._match_event(expected, observed2, match_fields)
        self.assertFalse(result2)

    def test_process_replace_fields(self):
        """Test processing __replace fields in expectations."""
        expectation = {
            "detail-type": "SequenceRunStateChange",
            "detail": {"instrumentRunId": "PLACEHOLDER"},
            "__replace": {
                "testInstrumentRunIdField": ["detail.instrumentRunId"],
            },
        }

        processed = verifier._process_replace_fields(
            expectation, self.test_instrument_run_id
        )

        self.assertEqual(
            processed["detail"]["instrumentRunId"], self.test_instrument_run_id
        )
        self.assertNotIn("__replace", processed)

    def test_get_nested_value(self):
        """Test getting nested values."""
        obj = {
            "detail": {
                "instrumentRunId": "260122_A00001_1234_TEST123456",
                "nested": {"value": "test"},
            }
        }

        self.assertEqual(
            verifier._get_nested_value(obj, "detail.instrumentRunId"),
            "260122_A00001_1234_TEST123456",
        )
        self.assertEqual(verifier._get_nested_value(obj, "detail.nested.value"), "test")
        self.assertIsNone(verifier._get_nested_value(obj, "detail.nonexistent"))
        self.assertIsNone(verifier._get_nested_value(obj, "nonexistent.path"))

    def test_apply_format(self):
        """Test format application."""
        result = verifier._apply_format("value", {"prefix": "pre_", "suffix": "_suf"})
        self.assertEqual(result, "pre_value_suf")

        result2 = verifier._apply_format("value", None)
        self.assertEqual(result2, "value")
