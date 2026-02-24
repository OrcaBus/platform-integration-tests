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

    def test_handler_requires_mode_and_service_name(self):
        """Test that handler raises error when mode or serviceName is missing."""
        # Missing mode
        event = {"serviceName": "sequencerunmanager"}
        with self.assertRaises(ValueError) as context:
            verifier.handler(event, None)
        self.assertIn("mode and serviceName are required", str(context.exception))

        # Missing serviceName
        event = {"mode": "status"}
        with self.assertRaises(ValueError) as context:
            verifier.handler(event, None)
        self.assertIn("mode and serviceName are required", str(context.exception))

    def test_handler_status_mode_returns_nested_result_for_single_service(self):
        """Test status mode returns result nested by service name for single service."""
        mock_result = {
            "status": "running",
            "runId": self.test_instrument_run_id,
            "observedEventsCount": 1,
            "expectedEventsCount": 2,
        }

        mock_service = MagicMock()
        mock_service.execute_check_run_status_process.return_value = mock_result

        event = {
            "serviceName": "sequencerunmanager",
            "seedResult": {
                "sequencerunmanager": {
                    "seedInstrumentRunId": self.test_instrument_run_id
                }
            },
            "mode": "status",
        }

        with patch(
            "lambdas.verifier.create_service_instance", return_value=mock_service
        ):
            result = verifier.handler(event, None)

        self.assertIn("sequencerunmanager", result)
        self.assertEqual(result["sequencerunmanager"]["status"], "running")
        self.assertEqual(
            result["sequencerunmanager"]["runId"], self.test_instrument_run_id
        )

    def test_handler_status_mode_returns_ready_for_single_service(self):
        """Test status mode returns ready when all events observed."""
        mock_result = {
            "status": "ready",
            "runId": self.test_instrument_run_id,
            "observedEventsCount": 2,
            "expectedEventsCount": 2,
        }

        mock_service = MagicMock()
        mock_service.execute_check_run_status_process.return_value = mock_result

        event = {
            "serviceName": "sequencerunmanager",
            "seedResult": {
                "sequencerunmanager": {
                    "seedInstrumentRunId": self.test_instrument_run_id
                }
            },
            "mode": "status",
        }

        with patch(
            "lambdas.verifier.create_service_instance", return_value=mock_service
        ):
            result = verifier.handler(event, None)

        self.assertEqual(result["sequencerunmanager"]["status"], "ready")

    def test_handler_verify_mode_returns_nested_result_for_single_service(self):
        """Test verify mode returns result nested by service name."""
        mock_result = {
            "runId": self.test_instrument_run_id,
            "runStatus": "passed",
            "matchedEventsCount": 2,
            "missingEventsCount": 0,
            "unexpectedEventsCount": 0,
            "totalExpectedEventsCount": 2,
        }

        mock_service = MagicMock()
        mock_service.execute_verify_process.return_value = mock_result

        event = {
            "serviceName": "sequencerunmanager",
            "seedResult": {
                "sequencerunmanager": {
                    "seedInstrumentRunId": self.test_instrument_run_id
                }
            },
            "mode": "verify",
        }

        with patch(
            "lambdas.verifier.create_service_instance", return_value=mock_service
        ):
            result = verifier.handler(event, None)

        self.assertIn("sequencerunmanager", result)
        self.assertEqual(result["sequencerunmanager"]["runStatus"], "passed")
        self.assertEqual(result["sequencerunmanager"]["matchedEventsCount"], 2)

    def test_handler_processes_all_services(self):
        """Test that handler processes all services when serviceName is 'all'."""
        mock_result_srm = {
            "status": "ready",
            "runId": "260122_A00001_1234_TESTSRM123",
            "observedEventsCount": 2,
            "expectedEventsCount": 2,
        }

        mock_service = MagicMock()
        mock_service.execute_check_run_status_process.return_value = mock_result_srm

        event = {
            "serviceName": "all",
            "seedResult": {
                "sequencerunmanager": {
                    "seedInstrumentRunId": "260122_A00001_1234_TESTSRM123"
                },
            },
            "mode": "status",
        }

        with patch(
            "lambdas.verifier.create_service_instance", return_value=mock_service
        ):
            result = verifier.handler(event, None)

        self.assertIsInstance(result, dict)
        self.assertGreater(len(result), 0)

    def test_handler_captures_error_per_service_for_all(self):
        """Test that handler captures errors per service when serviceName is 'all'."""
        mock_service_ok = MagicMock()
        mock_service_ok.execute_check_run_status_process.return_value = {
            "status": "ready",
            "runId": self.test_instrument_run_id,
        }

        mock_service_fail = MagicMock()
        mock_service_fail.execute_check_run_status_process.side_effect = Exception(
            "DynamoDB error"
        )

        def create_side_effect(service_name):
            if service_name == "sequencerunmanager":
                return mock_service_ok
            return mock_service_fail

        event = {
            "serviceName": "all",
            "seedResult": {
                "sequencerunmanager": {
                    "seedInstrumentRunId": self.test_instrument_run_id
                }
            },
            "mode": "status",
        }

        with patch(
            "lambdas.verifier.create_service_instance", side_effect=create_side_effect
        ):
            result = verifier.handler(event, None)

        self.assertIn("sequencerunmanager", result)
        self.assertEqual(result["sequencerunmanager"]["status"], "ready")
        for k, v in result.items():
            if k != "sequencerunmanager":
                self.assertIn("error", v)
