"""
Tests for collector.py lambda function.
"""

# Import base_test_case first to ensure environment variables are set
from test.base_test_case import LambdaTestCase

import json
import logging
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from lambdas import collector

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class CollectorTests(LambdaTestCase):
    """Test cases for collector lambda."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        super().setUp()

    def tearDown(self) -> None:
        """Clean up after tests."""
        super().tearDown()

    def test_handler_ignores_seed_events(self):
        """Test that collector ignores seed events."""
        event = {
            "version": "0",
            "id": "test-id",
            "source": "orcabus.integrationtests",
            "detail-type": "TestEvent",
            "detail": {"test": "data"},
        }

        result = collector.handler(event, None)

        self.assertEqual(result["ignored"], True)
        self.assertEqual(result["reason"], "seed_event")

    def test_handler_ignores_events_without_instrument_run_id(self):
        """Test that collector ignores events without instrument run ID."""
        event = {
            "version": "0",
            "id": "test-id",
            "source": "Pipe IcaEventPipeConstru-IntegrationTest",
            "detail-type": "UnknownEvent",
            "detail": {"test": "data"},
        }

        with patch("lambdas.collector.get_run_meta", return_value=None):
            result = collector.handler(event, None)

        self.assertEqual(result["ignored"], True)
        self.assertEqual(result["reason"], "no_instrument_run_id")

    def test_handler_ignores_events_without_run_meta(self):
        """Test that collector ignores events when run meta doesn't exist."""
        event = {
            "version": "0",
            "id": "test-id",
            "source": "Pipe IcaEventPipeConstru-IntegrationTest",
            "detail-type": "SequenceRunStateChange",
            "detail": {"instrumentRunId": "260122_A00001_1234_TEST123456"},
        }

        with patch("lambdas.collector.get_run_meta", return_value=None):
            result = collector.handler(event, None)

        self.assertEqual(result["ignored"], True)
        self.assertEqual(result["reason"], "no_run_meta")
        self.assertEqual(result["testInstrumentRunId"], "260122_A00001_1234_TEST123456")

    def test_handler_stores_event_successfully(self):
        """Test that collector stores event successfully."""
        test_instrument_run_id = "260122_A00001_1234_TEST123456"
        event = {
            "version": "0",
            "id": "test-event-id",
            "source": "Pipe IcaEventPipeConstru-IntegrationTest",
            "detail-type": "SequenceRunStateChange",
            "detail": {"instrumentRunId": test_instrument_run_id},
        }

        run_meta = {
            "testId": f"run#{test_instrument_run_id}",
            "sk": "run#meta",
            "status": "running",
        }

        with (
            patch("lambdas.collector.get_run_meta", return_value=run_meta),
            patch("lambdas.collector.store_item_to_s3", return_value=None) as mock_s3,
            patch(
                "lambdas.collector.put_item_to_dynamodb", return_value=None
            ) as mock_dynamodb,
        ):

            result = collector.handler(event, None)

        self.assertEqual(result["stored"], True)
        self.assertEqual(result["testInstrumentRunId"], test_instrument_run_id)
        self.assertIn("eventKey", result)
        self.assertEqual(result["eventKey"]["testId"], f"run#{test_instrument_run_id}")
        self.assertTrue(result["eventKey"]["sk"].startswith("event#"))

        # Verify S3 was called
        self.assertEqual(mock_s3.call_count, 1)

        # Verify DynamoDB was called
        self.assertEqual(mock_dynamodb.call_count, 1)
        call_args = mock_dynamodb.call_args[0][0]
        self.assertEqual(call_args["testId"], f"run#{test_instrument_run_id}")
        self.assertEqual(call_args["detailType"], "SequenceRunStateChange")
        self.assertEqual(
            call_args["source"], "Pipe IcaEventPipeConstru-IntegrationTest"
        )
        self.assertIsNotNone(call_args.get("payloadHash"))
        self.assertIsNotNone(call_args.get("rawS3Key"))
        self.assertIsNotNone(call_args.get("receivedAt"))

    def test_handler_handles_different_detail_types(self):
        """Test that collector handles different detail types correctly."""
        test_instrument_run_id = "260122_A00001_1234_TEST123456"
        event = {
            "version": "0",
            "id": "test-event-id",
            "source": "Pipe IcaEventPipeConstru-IntegrationTest",
            "detail-type": "SequenceRunSampleSheetChange",
            "detail": {"instrumentRunId": test_instrument_run_id},
        }

        run_meta = {
            "testId": f"run#{test_instrument_run_id}",
            "sk": "run#meta",
            "status": "running",
        }

        with (
            patch("lambdas.collector.get_run_meta", return_value=run_meta),
            patch("lambdas.collector.store_item_to_s3", return_value=None),
            patch("lambdas.collector.put_item_to_dynamodb", return_value=None),
        ):

            result = collector.handler(event, None)

        self.assertEqual(result["stored"], True)
        self.assertEqual(result["testInstrumentRunId"], test_instrument_run_id)

    def test_hash_payload(self):
        """Test payload hashing function."""
        payload = {"test": "data", "number": 123}
        hash1 = collector._hash_payload(payload)
        hash2 = collector._hash_payload(payload)

        # Same payload should produce same hash
        self.assertEqual(hash1, hash2)
        self.assertEqual(len(hash1), 64)  # SHA256 hex digest length

        # Different payload should produce different hash
        payload2 = {"test": "different"}
        hash3 = collector._hash_payload(payload2)
        self.assertNotEqual(hash1, hash3)

    def test_store_event_payload(self):
        """Test storing event payload to S3."""
        test_instrument_run_id = "260122_A00001_1234_TEST123456"
        event = {
            "version": "0",
            "id": "test-id",
            "detail-type": "SequenceRunStateChange",
            "detail": {"test": "data"},
        }

        with patch("lambdas.collector.store_item_to_s3") as mock_store:
            s3_key = collector._store_event_payload(test_instrument_run_id, event)

        # Verify S3 key format
        self.assertIn(f"events/testruns/{test_instrument_run_id}", s3_key)
        self.assertIn("year=", s3_key)
        self.assertIn("month=", s3_key)
        self.assertIn("day=", s3_key)
        self.assertIn("SequenceRunStateChange", s3_key)
        self.assertTrue(s3_key.endswith(".json"))

        # Verify store was called with correct arguments
        self.assertEqual(mock_store.call_count, 1)
        call_args = mock_store.call_args
        self.assertEqual(call_args[0][0], s3_key)
        self.assertIsInstance(json.loads(call_args[0][1]), dict)

    def test_get_instrument_run_id(self):
        """Test extracting instrument run ID from event."""
        event = {
            "detail-type": "SequenceRunStateChange",
            "detail": {"instrumentRunId": "260122_A00001_1234_TEST123456"},
        }

        instrument_run_id = collector._get_instrument_run_id(event)
        self.assertEqual(instrument_run_id, "260122_A00001_1234_TEST123456")

        # Test with unknown detail type
        event2 = {
            "detail-type": "UnknownEvent",
            "detail": {},
        }
        instrument_run_id2 = collector._get_instrument_run_id(event2)
        self.assertIsNone(instrument_run_id2)
