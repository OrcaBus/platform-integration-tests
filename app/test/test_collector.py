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
from utils.utils import get_event_test_id

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
        self.assertIn("seed_event", result["reason"])

    def test_handler_ignores_events_without_test_id(self):
        """Test that collector ignores events when test id cannot be extracted."""
        # Use valid detail-type but missing instrumentRunId so get_event_test_id returns None
        event = {
            "version": "0",
            "id": "test-id",
            "source": "Pipe IcaEventPipeConstru-IntegrationTest",
            "detail-type": "SequenceRunStateChange",
            "detail": {},  # no instrumentRunId
        }

        result = collector.handler(event, None)

        self.assertEqual(result["ignored"], True)
        self.assertIn("no test id", result["reason"])

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
        self.assertEqual(result["testId"], "260122_A00001_1234_TEST123456")

    def test_handler_stores_event_successfully(self):
        """Test that collector stores event successfully."""
        test_id = "260122_A00001_1234_TEST123456"
        event = {
            "version": "0",
            "id": "test-event-id",
            "source": "Pipe IcaEventPipeConstru-IntegrationTest",
            "detail-type": "SequenceRunStateChange",
            "detail": {"instrumentRunId": test_id},
        }

        run_meta = {
            "testId": f"run#{test_id}",
            "sk": "run#meta",
            "status": "running",
        }

        with (
            patch("lambdas.collector.get_run_meta", return_value=run_meta),
            patch("lambdas.collector.put_item_to_dynamodb") as mock_dynamodb,
        ):

            result = collector.handler(event, None)

        self.assertEqual(result["stored"], True)
        self.assertEqual(result["testId"], test_id)
        self.assertIn("eventKey", result)
        self.assertEqual(result["eventKey"]["testId"], test_id)
        self.assertTrue(result["eventKey"]["sk"].startswith("event#"))

        self.assertEqual(mock_dynamodb.call_count, 1)
        call_args = mock_dynamodb.call_args[0][0]
        self.assertEqual(call_args["testId"], test_id)
        self.assertEqual(call_args["detailType"], "SequenceRunStateChange")
        self.assertEqual(
            call_args["source"], "Pipe IcaEventPipeConstru-IntegrationTest"
        )
        self.assertIn("observedEvent", call_args)
        self.assertEqual(call_args["observedEvent"], event)
        self.assertIn("receivedAt", call_args)

    def test_handler_handles_different_detail_types(self):
        """Test that collector handles different detail types correctly."""
        test_id = "260122_A00001_1234_TEST123456"
        event = {
            "version": "0",
            "id": "test-event-id",
            "source": "Pipe IcaEventPipeConstru-IntegrationTest",
            "detail-type": "SequenceRunSampleSheetChange",
            "detail": {"instrumentRunId": test_id},
        }

        run_meta = {
            "testId": f"run#{test_id}",
            "sk": "run#meta",
            "status": "running",
        }

        with (
            patch("lambdas.collector.get_run_meta", return_value=run_meta),
            patch("lambdas.collector.put_item_to_dynamodb", return_value=None),
        ):

            result = collector.handler(event, None)

        self.assertEqual(result["stored"], True)
        self.assertEqual(result["testId"], test_id)

    def test_store_event_payload(self):
        """Test storing event payload to S3."""
        test_id = "260122_A00001_1234_TEST123456"
        event = {
            "version": "0",
            "id": "test-id",
            "detail-type": "SequenceRunStateChange",
            "detail": {"instrumentRunId": test_id},
        }

        with patch("lambdas.collector.store_item_to_s3") as mock_store:
            s3_key = collector._store_event_payload(event)

        self.assertIn(f"events/testruns/", s3_key)
        self.assertIn(test_id, s3_key)
        self.assertIn("year=", s3_key)
        self.assertIn("month=", s3_key)
        self.assertIn("day=", s3_key)
        self.assertIn("SequenceRunStateChange", s3_key)
        self.assertTrue(s3_key.endswith(".json"))

        self.assertEqual(mock_store.call_count, 1)
        call_args = mock_store.call_args
        self.assertEqual(call_args[0][0], s3_key)
        self.assertIsInstance(json.loads(call_args[0][1]), dict)

    def test_get_event_test_id(self):
        """Test extracting test id from event using utils.get_event_test_id."""
        event = {
            "detail-type": "SequenceRunStateChange",
            "detail": {"instrumentRunId": "260122_A00001_1234_TEST123456"},
        }

        test_id = get_event_test_id(event)
        self.assertEqual(test_id, "260122_A00001_1234_TEST123456")

        # Test with unknown detail type (not in TEST_ID_MAPPING)
        event2 = {
            "detail-type": "UnknownEvent",
            "detail": {},
        }
        with self.assertRaises(ValueError):
            get_event_test_id(event2)
