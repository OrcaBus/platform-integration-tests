"""
Base test case class for Lambda function tests.
Provides common setup and teardown for mocking AWS services.
"""

import logging
import os
import uuid
from unittest import TestCase
from mockito import when, unstub, mock
from unittest.mock import patch
import boto3

# Set environment variables at module level before any imports that might use them
# This ensures services modules can access these variables when imported
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("EVENT_BUS_NAME", "test-event-bus")
os.environ.setdefault("RULE_NAME", "test-rule")

# Set dummy AWS credentials to prevent boto3 from trying to load real credentials
# These are only used during import - actual tests will use mocked clients
# Use explicit assignment to ensure they override any existing values
if "AWS_ACCESS_KEY_ID" not in os.environ:
    os.environ["AWS_ACCESS_KEY_ID"] = "test-access-key"
if "AWS_SECRET_ACCESS_KEY" not in os.environ:
    os.environ["AWS_SECRET_ACCESS_KEY"] = "test-secret-key"  # pragma: allowlist secret
if "AWS_SESSION_TOKEN" not in os.environ:
    os.environ["AWS_SESSION_TOKEN"] = "test-session-token"

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class LambdaTestCase(TestCase):
    """Base test case for Lambda function tests."""

    def setUp(self) -> None:
        """Set up test environment with mocked AWS services."""
        super().setUp()

        # Set AWS region
        os.environ["AWS_DEFAULT_REGION"] = "ap-southeast-2"
        os.environ["TABLE_NAME"] = "test-table"
        os.environ["S3_BUCKET"] = "test-bucket"
        os.environ["EVENT_BUS_NAME"] = "test-event-bus"
        os.environ["RULE_NAME"] = "test-rule"

        # Mock DynamoDB client
        mock_dynamodb = boto3.client(
            "dynamodb",
            endpoint_url="http://localhost:4566",
            region_name="ap-southeast-2",
            aws_access_key_id=str(uuid.uuid4()),
            aws_secret_access_key=str(uuid.uuid4()),
            aws_session_token=f"{uuid.uuid4()}_{uuid.uuid4()}",
        )
        mock_dynamodb_resource = boto3.resource(
            "dynamodb",
            endpoint_url="http://localhost:4566",
            region_name="ap-southeast-2",
            aws_access_key_id=str(uuid.uuid4()),
            aws_secret_access_key=str(uuid.uuid4()),
            aws_session_token=f"{uuid.uuid4()}_{uuid.uuid4()}",
        )

        # Mock S3 client
        mock_s3 = boto3.client(
            "s3",
            endpoint_url="http://localhost:4566",
            region_name="ap-southeast-2",
            aws_access_key_id=str(uuid.uuid4()),
            aws_secret_access_key=str(uuid.uuid4()),
            aws_session_token=f"{uuid.uuid4()}_{uuid.uuid4()}",
        )

        # Mock EventBridge client
        mock_eb = boto3.client(
            "events",
            endpoint_url="http://localhost:4566",
            region_name="ap-southeast-2",
            aws_access_key_id=str(uuid.uuid4()),
            aws_secret_access_key=str(uuid.uuid4()),
            aws_session_token=f"{uuid.uuid4()}_{uuid.uuid4()}",
        )

        # Patch boto3 clients in services - patch the actual client/resource objects
        # Note: These patches need to be applied before the services modules are imported
        # For tests, we'll patch the service functions directly instead
        self.dynamodb_table_patcher = patch(
            "services.dynamodb.table", mock_dynamodb_resource.Table("test-table")
        )
        self.s3_patcher = patch("services.s3.s3", mock_s3)
        self.eb_patcher = patch("services.eventbridge.events_client", mock_eb)

        self.dynamodb_table_patcher.start()
        self.s3_patcher.start()
        self.eb_patcher.start()

        # Store mock clients for use in tests
        self.mock_dynamodb = mock_dynamodb
        self.mock_dynamodb_resource = mock_dynamodb_resource
        self.mock_s3 = mock_s3
        self.mock_eb = mock_eb

    def tearDown(self) -> None:
        """Clean up test environment."""
        # Clean up environment variables
        env_vars_to_remove = [
            "AWS_DEFAULT_REGION",
            "TABLE_NAME",
            "S3_BUCKET",
            "EVENT_BUS_NAME",
            "RULE_NAME",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
        ]
        for var in env_vars_to_remove:
            if var in os.environ:
                del os.environ[var]

        # Stop patchers
        self.dynamodb_table_patcher.stop()
        self.s3_patcher.stop()
        self.eb_patcher.stop()

        # Unstub mockito
        unstub()
        super().tearDown()
