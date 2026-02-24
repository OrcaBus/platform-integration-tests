"""
S3 operations for the integration testing service.
Contains functions for interacting with S3 buckets.
"""

from typing import Dict, Any, List, Tuple
import logging
import os
import boto3
import json

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# S3 configuration
S3_BUCKET = os.environ["S3_BUCKET"]
s3 = boto3.client("s3")


def get_s3_keys_for_service(service_name: str) -> Tuple[str, str]:
    """
    Return (seeds_key, expectations_key) for a given serviceName.

    Args:
        service_name: The service name

    Returns:
        Tuple of (seeds_key, expectations_key)

    Layout:
      seed/services/{serviceName}/seeds.json
      seed/services/{serviceName}/expectations.json
    """
    base_prefix = f"seed/services/{service_name}/"
    return (
        base_prefix + "seeds.json",
        base_prefix + "expectations.json",
    )


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


def load_s3_json(key: str) -> Dict[str, Any]:
    """
    Load JSON from S3.
    If the object does not exist, raise ClientError with NoSuchKey.
    """
    data = get_item_from_s3(key)
    try:
        return json.loads(data)
    except Exception as e:
        logger.error(f"Failed to load JSON from S3: {e}")
        raise e


def load_s3_json_list(key: str) -> List[Dict[str, Any]]:
    """
    Load JSON list from S3.
    If the object does not exist, raise ClientError with NoSuchKey.
    """
    data = load_s3_json(key)
    if isinstance(data, list):
        return data
    else:
        raise ValueError(f"JSON file {key} must contain a JSON array")


def load_service_seed_definitions(
    service_name: str,
) -> List[Dict[str, Any]]:
    """
    Try to load events for the requested serviceName.
    If those keys don't exist, fall back to 'all'.
    Returns list of seed definitions.
    """
    seeds_key, _ = get_s3_keys_for_service(service_name)

    try:
        seeds = load_s3_json_list(seeds_key)
        logger.info("Loaded %d seeds for serviceName=%s", len(seeds), service_name)
        return seeds
    except Exception as e:
        logger.error(f"Failed to load seeds for serviceName={service_name}: {e}")
        raise e


def load_service_expectations(service_name: str) -> List[Dict[str, Any]]:
    """
    Load expectations for the requested serviceName.
    """
    expectations_key = f"seed/services/{service_name}/expectations.json"
    return load_s3_json_list(expectations_key)
