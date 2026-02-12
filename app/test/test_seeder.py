"""
Tests for seeder.py lambda function.
"""

# Import base_test_case first to ensure environment variables are set
from test.base_test_case import LambdaTestCase

import json
import logging
from unittest.mock import patch, MagicMock

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

    def test_handler_creates_run_meta_and_publishes_events_for_single_service(self):
        """Test that handler creates run meta and publishes events for single service."""
        mock_result = {
            "seedInstrumentRunId": "260122_A00001_1234_TESTSRM123",
            "serviceName": "sequencerunmanager",
            "startedAt": "2025-01-01T00:00:00Z",
            "timeoutAt": "2025-01-01T00:05:00Z",
            "publishedCount": 2,
        }

        mock_service = MagicMock()
        mock_service.execute_seed_process.return_value = mock_result

        event = {
            "Payload": {
                "serviceName": "sequencerunmanager",
            }
        }

        with patch("lambdas.seeder.create_service_instance", return_value=mock_service):
            result = seeder.handler(event, None)

        # Verify result structure (nested by service name)
        self.assertIn("sequencerunmanager", result)
        self.assertEqual(result["sequencerunmanager"], mock_result)
        self.assertEqual(
            result["sequencerunmanager"]["seedInstrumentRunId"],
            mock_result["seedInstrumentRunId"],
        )
        self.assertEqual(result["sequencerunmanager"]["publishedCount"], 2)

        mock_service.execute_seed_process.assert_called_once()

    def test_handler_processes_all_services(self):
        """Test that handler processes all registered services when serviceName is 'all'."""
        mock_result_srm = {
            "seedInstrumentRunId": "260122_A00001_1234_TESTSRM123",
            "serviceName": "sequencerunmanager",
            "startedAt": "2025-01-01T00:00:00Z",
            "timeoutAt": "2025-01-01T00:05:00Z",
            "publishedCount": 2,
        }

        mock_service_srm = MagicMock()
        mock_service_srm.execute_seed_process.return_value = mock_result_srm

        event = {
            "Payload": {
                "serviceName": "all",
            }
        }

        with patch(
            "lambdas.seeder.create_service_instance", return_value=mock_service_srm
        ):
            result = seeder.handler(event, None)

        # For "all", result is dict keyed by service name
        self.assertIsInstance(result, dict)
        # Should have results for each service in services_to_process (AVAILABLE_SERVICES minus "all")
        self.assertGreater(len(result), 0)
        for service_name, service_result in result.items():
            self.assertIn("seedInstrumentRunId", service_result)
            self.assertIn("serviceName", service_result)
            self.assertIn("publishedCount", service_result)

    def test_handler_raises_for_unregistered_service(self):
        """Test that handler raises when service is in AVAILABLE_SERVICES but not in registry."""
        # workflowrunmanager is in AVAILABLE_SERVICES but not in SERVICE_REGISTRY
        event = {
            "Payload": {
                "serviceName": "workflowrunmanager",
            }
        }

        with patch(
            "lambdas.seeder.create_service_instance",
            side_effect=ValueError("Service 'workflowrunmanager' is not registered"),
        ):
            with self.assertRaises(ValueError) as context:
                seeder.handler(event, None)

        self.assertIn("not registered", str(context.exception))

    def test_handler_handles_service_error_gracefully_for_all(self):
        """Test that handler captures errors per service when serviceName is 'all'."""
        mock_result_ok = {
            "seedInstrumentRunId": "260122_A00001_1234_TESTSRM123",
            "serviceName": "sequencerunmanager",
            "publishedCount": 2,
        }

        mock_service_ok = MagicMock()
        mock_service_ok.execute_seed_process.return_value = mock_result_ok

        mock_service_fail = MagicMock()
        mock_service_fail.execute_seed_process.side_effect = Exception("S3 error")

        def create_side_effect(service_name):
            if service_name == "sequencerunmanager":
                return mock_service_ok
            return mock_service_fail

        event = {
            "Payload": {
                "serviceName": "all",
            }
        }

        with patch(
            "lambdas.seeder.create_service_instance", side_effect=create_side_effect
        ):
            result = seeder.handler(event, None)

        # Should have results for all services; failed ones have error key
        self.assertIsInstance(result, dict)
        self.assertIn("sequencerunmanager", result)
        self.assertEqual(
            result["sequencerunmanager"]["seedInstrumentRunId"],
            mock_result_ok["seedInstrumentRunId"],
        )
        # Other services may have {"error": "..."}
        for k, v in result.items():
            if k != "sequencerunmanager":
                self.assertIn("error", v)

    def test_handler_raises_for_unsupported_service_name(self):
        """Test that handler raises when service name is not in AVAILABLE_SERVICES."""
        event = {
            "Payload": {
                "serviceName": "invalid_service_xyz",
            }
        }

        with self.assertRaises(ValueError) as context:
            seeder.handler(event, None)

        self.assertIn("not supported", str(context.exception))
