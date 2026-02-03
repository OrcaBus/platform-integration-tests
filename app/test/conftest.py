"""
Pytest configuration and fixtures for Lambda function tests.
This file is automatically loaded by pytest before test collection.
"""

import os
import tempfile

# Set environment variables before any test modules are imported
# This ensures services modules can access these variables when imported
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("EVENT_BUS_NAME", "test-event-bus")
os.environ.setdefault("RULE_NAME", "test-rule")

# Set dummy AWS credentials to prevent boto3 from trying to load real credentials
# These are only used during import - actual tests will use mocked clients
# Use explicit values (not setdefault) to override any existing partial credentials
os.environ["AWS_ACCESS_KEY_ID"] = "test-access-key"
os.environ["AWS_SECRET_ACCESS_KEY"] = "test-secret-key"  # pragma: allowlist secret
os.environ["AWS_SESSION_TOKEN"] = "test-session-token"

# Create a temporary credentials file with complete dummy credentials
# This prevents boto3 from loading partial credentials from actual credential files
_temp_creds_file = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".ini")
_temp_creds_file.write("""[default]
aws_access_key_id = test-access-key
aws_secret_access_key = test-secret-key
aws_session_token = test-session-token
""")
_temp_creds_file.close()
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = _temp_creds_file.name

# Also set config file to a non-existent path to avoid any config issues
_nonexistent_config = os.path.join(
    tempfile.gettempdir(), "pytest-aws-config-nonexistent"
)
os.environ["AWS_CONFIG_FILE"] = _nonexistent_config
