"""
Tests for seeder.py lambda function.
"""

# Import base_test_case first to ensure environment variables are set
from test.base_test_case import LambdaTestCase

import json
import logging
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
from botocore.exceptions import ClientError

from lambdas import seeder

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class SeederTests(LambdaTestCase):
    """Test cases for seeder lambda."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        super().setUp()

    def tearDown(self) -> None:
        """Clean up after tests."""
        super().tearDown()

    def test_handler_requires_payload(self):
        """Test that handler raises error when payload is missing."""
        event = {}

        with self.assertRaises(ValueError) as context:
            seeder.handler(event, None)

        self.assertIn("payload is required", str(context.exception))

    def test_handler_creates_run_meta_and_publishes_events(self):
        """Test that handler creates run meta and publishes events."""
        test_seeds = [
            {
                "source": "Pipe IcaEventPipeConstru-IntegrationTest",
                "detail-type": "SequenceRunStateChange",
                "detail": {"instrumentRunId": "PLACEHOLDER"},
                "__replace": {
                    "testInstrumentRunIdField": ["detail.instrumentRunId"],
                },
            }
        ]

        event = {
            "Payload": {
                "serviceName": "all",
            }
        }

        with (
            patch(
                "lambdas.seeder.get_item_from_s3", return_value=json.dumps(test_seeds)
            ),
            patch("lambdas.seeder.put_item_to_dynamodb") as mock_put_meta,
            patch("lambdas.seeder.put_event_to_event_bus") as mock_put_event,
            patch(
                "lambdas.seeder.get_s3_keys_for_service",
                return_value=(
                    "seed/services/all/seeds.json",
                    "seed/services/all/expectations.json",
                ),
            ),
        ):

            result = seeder.handler(event, None)

        # Verify run meta was created
        self.assertEqual(mock_put_meta.call_count, 1)
        meta_call = mock_put_meta.call_args[0][0]
        self.assertEqual(meta_call["sk"], "run#meta")
        self.assertIn("testId", meta_call)
        self.assertEqual(meta_call["status"], "running")
        self.assertIn("startedAt", meta_call)
        self.assertIn("timeoutAt", meta_call)
        self.assertEqual(meta_call["serviceName"], "all")

        # Verify events were published
        self.assertEqual(mock_put_event.call_count, 1)

        # Verify result
        self.assertIn("testInstrumentRunId", result)
        self.assertEqual(result["serviceName"], "all")
        self.assertIn("startedAt", result)
        self.assertIn("timeoutAt", result)
        self.assertEqual(result["publishedCount"], 1)

    def test_handler_falls_back_to_all_service(self):
        """Test that handler falls back to 'all' service when specific service S3 file not found."""
        test_seeds = [
            {
                "source": "Pipe IcaEventPipeConstru-IntegrationTest",
                "detail-type": "SequenceRunStateChange",
                "detail": {"instrumentRunId": "PLACEHOLDER"},
            }
        ]

        # Use a valid service name that doesn't have an S3 file (e.g., "bclconvertermanager")
        # The fallback happens when the S3 file doesn't exist, not when service name is invalid
        event = {
            "Payload": {
                "serviceName": "bclconvertermanager",  # Valid service name but no S3 file
            }
        }

        # First call raises NoSuchKey, second call (fallback) succeeds
        def side_effect(key):
            if "bclconvertermanager" in key:
                error = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
                raise error
            return json.dumps(test_seeds)

        with (
            patch("lambdas.seeder.get_item_from_s3", side_effect=side_effect),
            patch("lambdas.seeder.put_item_to_dynamodb"),
            patch("lambdas.seeder.put_event_to_event_bus"),
            patch("lambdas.seeder.get_s3_keys_for_service") as mock_get_keys,
        ):

            # Mock get_s3_keys_for_service to return different keys
            def get_keys_side_effect(service_name):
                if service_name == "bclconvertermanager":
                    return (
                        "seed/services/bclconvertermanager/seeds.json",
                        "seed/services/bclconvertermanager/expectations.json",
                    )
                return (
                    "seed/services/all/seeds.json",
                    "seed/services/all/expectations.json",
                )

            mock_get_keys.side_effect = get_keys_side_effect

            result = seeder.handler(event, None)

        # Should fall back to 'all' service
        self.assertEqual(result["serviceName"], "all")

    def test_generate_test_instrument_run_id(self):
        """Test test instrument run ID generation."""
        run_id = seeder._generate_test_instrument_run_id("SRM")

        # Verify format: YYMMDD_A00001_XXXX_TESTSRMXXX
        parts = run_id.split("_")
        self.assertEqual(len(parts), 4)
        self.assertEqual(len(parts[0]), 6)  # YYMMDD
        self.assertEqual(parts[1], "A00001")
        self.assertEqual(len(parts[2]), 4)  # XXXX
        self.assertTrue(parts[3].startswith("TESTSRM"))
        self.assertEqual(len(parts[3]), 10)  # TESTSRMXXX

    def test_load_s3_json_list(self):
        """Test loading JSON list from S3."""
        test_data = [{"test": "data1"}, {"test": "data2"}]

        with patch(
            "lambdas.seeder.get_item_from_s3", return_value=json.dumps(test_data)
        ):
            result = seeder._load_s3_json_list("test-key")

        self.assertEqual(result, test_data)

    def test_load_s3_json_list_raises_error_for_non_list(self):
        """Test that loading non-list JSON raises error."""
        test_data = {"test": "data"}

        with patch(
            "lambdas.seeder.get_item_from_s3", return_value=json.dumps(test_data)
        ):
            with self.assertRaises(ValueError) as context:
                seeder._load_s3_json_list("test-key")

            self.assertIn("must contain a JSON array", str(context.exception))

    def test_apply_format(self):
        """Test format application."""
        # Test with prefix
        result = seeder._apply_format("value", {"prefix": "pre_"})
        self.assertEqual(result, "pre_value")

        # Test with suffix
        result = seeder._apply_format("value", {"suffix": "_suf"})
        self.assertEqual(result, "value_suf")

        # Test with both
        result = seeder._apply_format("value", {"prefix": "pre_", "suffix": "_suf"})
        self.assertEqual(result, "pre_value_suf")

        # Test without format config
        result = seeder._apply_format("value", None)
        self.assertEqual(result, "value")

    def test_publish_test_events(self):
        """Test publishing test events."""
        test_instrument_run_id = "260122_A00001_1234_TEST123456"
        test_sequence_run_id = "test-abcdefghijklmnopqrstuvwxyz"
        service_name = "all"
        events_definitions = [
            {
                "source": "Pipe IcaEventPipeConstru-IntegrationTest",
                "detail-type": "SequenceRunStateChange",
                "detail": {"instrumentRunId": "PLACEHOLDER"},
                "__replace": {
                    "testInstrumentRunIdField": ["detail.instrumentRunId"],
                },
            },
            {
                "source": "Pipe IcaEventPipeConstru-IntegrationTest",
                "detail-type": "SequenceRunSampleSheetChange",
                "detail": {"instrumentRunId": "PLACEHOLDER"},
                "__replace": {
                    "testInstrumentRunIdField": ["detail.instrumentRunId"],
                },
            },
        ]

        with (
            patch("lambdas.seeder.put_event_to_event_bus") as mock_put_event,
            patch("lambdas.seeder.time.sleep"),
        ):  # Mock sleep to speed up tests

            count = seeder._publish_test_events(
                test_instrument_run_id, test_sequence_run_id, service_name, events_definitions
            )

        # Verify both events were published
        self.assertEqual(count, 2)
        self.assertEqual(mock_put_event.call_count, 2)

        # Verify first event was published with correct instrument run ID
        first_call = mock_put_event.call_args_list[0][0][0]
        self.assertEqual(first_call["EventBusName"], "test-event-bus")
        self.assertEqual(
            first_call["Source"], "Pipe IcaEventPipeConstru-IntegrationTest"
        )
        self.assertEqual(first_call["DetailType"], "SequenceRunStateChange")

        # Parse detail to verify replacement
        detail = json.loads(first_call["Detail"])
        self.assertEqual(detail["instrumentRunId"], test_instrument_run_id)

    def test_publish_test_events_with_timestamp_replacement(self):
        """Test publishing events with timestamp replacement."""
        test_instrument_run_id = "260122_A00001_1234_TEST123456"
        test_sequence_run_id = "test-sequence-run-id-12345"
        service_name = "all"
        events_definitions = [
            {
                "source": "Pipe IcaEventPipeConstru-IntegrationTest",
                "detail-type": "SequenceRunStateChange",
                "time": "PLACEHOLDER",
                "detail": {"dateModified": "PLACEHOLDER"},
                "__replace": {
                    "testInstrumentRunIdField": ["detail.instrumentRunId"],
                    "timeStampField": ["time", "detail.dateModified"],
                },
            }
        ]

        with (
            patch("lambdas.seeder.put_event_to_event_bus") as mock_put_event,
            patch("lambdas.seeder.time.sleep"),
        ):

            count = seeder._publish_test_events(
                test_instrument_run_id, test_sequence_run_id, service_name, events_definitions
            )

        self.assertEqual(count, 1)
        call_args = mock_put_event.call_args[0][0]
        self.assertIsNotNone(call_args.get("Time"))
        detail = json.loads(call_args["Detail"])
        self.assertIsNotNone(detail.get("dateModified"))

    def test_publish_test_events_with_random_unique_id(self):
        """Test publishing events with random unique ID replacement."""
        test_instrument_run_id = "260122_A00001_1234_TEST123456"
        test_sequence_run_id = "test-sequence-run-id-12345"
        service_name = "all"
        events_definitions = [
            {
                "source": "Pipe IcaEventPipeConstru-IntegrationTest",
                "detail-type": "SequenceRunStateChange",
                "detail": {"id": "PLACEHOLDER"},
                "__replace": {
                    "randomUniqueIdField": [
                        {"name": "detail.id", "format": {"prefix": "r."}}
                    ],
                },
            }
        ]

        with (
            patch("lambdas.seeder.put_event_to_event_bus") as mock_put_event,
            patch("lambdas.seeder.time.sleep"),
        ):

            count = seeder._publish_test_events(
                test_instrument_run_id, test_sequence_run_id, service_name, events_definitions
            )

        self.assertEqual(count, 1)
        call_args = mock_put_event.call_args[0][0]
        detail = json.loads(call_args["Detail"])
        # Verify ID was replaced and has prefix
        self.assertIsNotNone(detail.get("id"))
        self.assertTrue(detail["id"].startswith("r."))
        self.assertNotEqual(detail["id"], "PLACEHOLDER")

    def test_publish_test_events_removes_replace_field(self):
        """Test that __replace field is removed before publishing."""
        test_instrument_run_id = "260122_A00001_1234_TEST123456"
        test_sequence_run_id = "test-sequence-run-id-12345"
        service_name = "all"
        events_definitions = [
            {
                "source": "Pipe IcaEventPipeConstru-IntegrationTest",
                "detail-type": "SequenceRunStateChange",
                "detail": {"instrumentRunId": "PLACEHOLDER"},
                "__replace": {
                    "testInstrumentRunIdField": ["detail.instrumentRunId"],
                },
            }
        ]

        with (
            patch("lambdas.seeder.put_event_to_event_bus") as mock_put_event,
            patch("lambdas.seeder.time.sleep"),
        ):

            seeder._publish_test_events(
                test_instrument_run_id, test_sequence_run_id, service_name, events_definitions
            )

        call_args = mock_put_event.call_args[0][0]
        detail = json.loads(call_args["Detail"])
        # Verify __replace was removed
        self.assertNotIn("__replace", detail)

    def test_now_iso(self):
        """Test ISO timestamp generation."""
        timestamp = seeder._now_iso()
        self.assertTrue(timestamp.endswith("Z"))
        self.assertIn("T", timestamp)
        # Should be parseable
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        self.assertIsNotNone(parsed)
