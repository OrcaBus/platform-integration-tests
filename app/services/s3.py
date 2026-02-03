"""
S3 operations for the integration testing service.
Contains functions for interacting with S3 buckets.
"""

from typing import Dict, Any
import logging
import os
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# S3 configuration
S3_BUCKET = os.environ["S3_BUCKET"]
s3 = boto3.client("s3")


def store_item_to_s3(key: str, body: str) -> Dict[str, Any]:
    """
    Store item to S3.

    Args:
        key: The S3 key (path) where the item should be stored
        body: The content to store (will be encoded as UTF-8)

    Returns:
        The response from S3

    Example:
        >>> store_item_to_s3("events/test.json", '{"test": "data"}')
    """
    try:
        return s3.put_object(Bucket=S3_BUCKET, Key=key, Body=body.encode("utf-8"))
    except Exception as e:
        logger.error(f"Failed to store item to S3: {e}")
        raise e


def get_item_from_s3(key: str) -> str:
    """
    Get item from S3.

    Args:
        key: The S3 key (path) of the item to retrieve

    Returns:
        The content as a decoded string

    Example:
        >>> content = get_item_from_s3("seed/services/all/seeds.json")
        >>> data = json.loads(content)
    """
    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=key)
        return response["Body"].read().decode("utf-8")
    except Exception as e:
        logger.error(f"Failed to get item from S3: {e}")
        raise e
