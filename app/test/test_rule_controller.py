"""
Tests for rule_controller.py lambda function.
"""

# Import base_test_case first to ensure environment variables are set
from test.base_test_case import LambdaTestCase

import logging
from unittest.mock import patch, MagicMock

from lambdas import rule_controller

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class RuleControllerTests(LambdaTestCase):
    """Test cases for rule_controller lambda."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        super().setUp()

    def tearDown(self) -> None:
        """Clean up after tests."""
        super().tearDown()

    def test_handler_enables_rule(self):
        """Test that handler enables EventBridge rule."""
        event = {
            "action": "enable",
            "serviceName": "workflowrunmanager",
        }

        with patch("lambdas.rule_controller.events_client") as mock_events:
            result = rule_controller.handler(event, None)

        # Verify enable_rule was called
        mock_events.enable_rule.assert_called_once_with(
            Name="test-rule", EventBusName="test-event-bus"
        )

        # Verify result
        self.assertEqual(result["action"], "enable")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["ruleName"], "test-rule")
        self.assertEqual(result["eventBusName"], "test-event-bus")
        self.assertEqual(result["serviceName"], "workflowrunmanager")

    def test_handler_disables_rule(self):
        """Test that handler disables EventBridge rule."""
        event = {
            "action": "disable",
            "serviceName": "all",
        }

        with patch("lambdas.rule_controller.events_client") as mock_events:
            result = rule_controller.handler(event, None)

        # Verify disable_rule was called
        mock_events.disable_rule.assert_called_once_with(
            Name="test-rule", EventBusName="test-event-bus"
        )

        # Verify result
        self.assertEqual(result["action"], "disable")
        self.assertEqual(result["status"], "ok")

    def test_handler_raises_error_for_invalid_action(self):
        """Test that handler raises error for invalid action."""
        event = {
            "action": "invalid",
            "serviceName": "all",
        }

        with self.assertRaises(ValueError) as context:
            rule_controller.handler(event, None)

        self.assertIn("Unsupported action", str(context.exception))

    def test_handler_handles_errors(self):
        """Test that handler properly handles errors."""
        event = {
            "action": "enable",
            "serviceName": "all",
        }

        with patch("lambdas.rule_controller.events_client") as mock_events:
            mock_events.enable_rule.side_effect = Exception("Test error")

            with self.assertRaises(Exception) as context:
                rule_controller.handler(event, None)

            self.assertIn("Test error", str(context.exception))

    def test_handler_defaults_service_name(self):
        """Test that handler defaults serviceName to 'all'."""
        event = {
            "action": "enable",
        }

        with patch("lambdas.rule_controller.events_client") as mock_events:
            result = rule_controller.handler(event, None)

        self.assertEqual(result["serviceName"], "all")
