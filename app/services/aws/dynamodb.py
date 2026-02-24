"""
DynamoDB operations for the integration testing service.
Contains functions for interacting with the DynamoDB table.
"""

from typing import List, Dict, Optional, Any
import logging
import os
import boto3
from boto3.dynamodb.conditions import Key, Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# DynamoDB configuration
TABLE_NAME = os.environ["TABLE_NAME"]
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def get_run_meta(test_id: str) -> Optional[Dict[str, Any]]:
    """
    Get run meta from DynamoDB.

    Args:
        test_id: The test ID (format: run#YYMMDD_A00001_XXXX_TESTXXXXXX)

    Returns:
        The run meta item if found, None otherwise

    Example with instrument run id:
        >>> meta = get_run_meta("run#260122_A00001_1234_TEST123456")
        >>> print(meta["status"])
        "running"
    """
    if not test_id:
        logger.error(f"test_id is required")
        raise ValueError("test_id is required")
    key = {"testId": f"run#{test_id}", "sk": "run#meta"}
    return get_item_from_dynamodb(key)


def put_item_to_dynamodb(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Put item to DynamoDB.

    Args:
        item: The item to put (must include partition key and sort key)

    Returns:
        The response from DynamoDB
    """
    try:
        return table.put_item(Item=item)
    except Exception as e:
        logger.error(f"Failed to put item to DynamoDB: {e}")
        raise e


def get_item_from_dynamodb(key: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Get item from DynamoDB.

    Args:
        key: Dictionary containing the partition key and sort key

    Returns:
        The item if found, None otherwise
    """
    try:
        response = table.get_item(Key=key)
        return response.get("Item")
    except Exception as e:
        logger.error(f"Failed to get item from DynamoDB: {e}")
        raise e


def get_items_from_dynamodb(
    KeyConditionExpression: Any, FilterExpression: Optional[Any] = None
) -> List[Dict[str, Any]]:
    """
    Query items from DynamoDB.

    Args:
        KeyConditionExpression: A boto3.dynamodb.conditions.Key condition expression
        FilterExpression: Optional boto3.dynamodb.conditions.Attr filter expression

    Returns:
        List of items matching the query conditions

    Example:
        >>> from boto3.dynamodb.conditions import Key, Attr
        >>> items = get_items_from_dynamodb(
        ...     KeyConditionExpression=Key("testId").eq("run#123")
        ...     & Key("sk").begins_with("event#"),
        ...     FilterExpression=Attr("status").eq("matched")
        ... )
    """
    try:
        kwargs = {"KeyConditionExpression": KeyConditionExpression}
        if FilterExpression is not None:
            kwargs["FilterExpression"] = FilterExpression
        query_result = table.query(**kwargs)
        return query_result.get("Items", [])
    except Exception as e:
        logger.error(f"Failed to query items from DynamoDB: {e}")
        raise e


def update_item_to_dynamodb(
    key: Dict[str, Any],
    UpdateExpression: str,
    ExpressionAttributeNames: Dict[str, str],
    ExpressionAttributeValues: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Update item in DynamoDB.

    Args:
        key: Dictionary containing the partition key and sort key
        UpdateExpression: The update expression (e.g., "SET #status = :status")
        ExpressionAttributeNames: Dictionary mapping expression attribute names
        ExpressionAttributeValues: Dictionary mapping expression attribute values

    Returns:
        The response from DynamoDB
    """
    try:
        return table.update_item(
            Key=key,
            UpdateExpression=UpdateExpression,
            ExpressionAttributeNames=ExpressionAttributeNames,
            ExpressionAttributeValues=ExpressionAttributeValues,
        )
    except Exception as e:
        logger.error(f"Failed to update item in DynamoDB: {e}")
        raise e


def get_observed_events(
    test_id: str, detail_type: str, source: str
) -> List[Dict[str, Any]]:
    """
    Query DynamoDB for observed events matching test_id, detail_type, and source.
    Returns list of dynamoDB event items (with rawS3Key).

    Args:
        test_id: The test ID (with format: "run#YYMMDD_A00001_XXXX_TESTXXXXXX")
        detail_type: The event detail type (empty string to get all events)
        source: The event source (empty string to get all events)
    Returns:
        List of dynamoDB event items (with rawS3Key).
    """
    if detail_type and source:
        return get_items_from_dynamodb(
            KeyConditionExpression=Key("testId").eq(test_id)
            & Key("sk").begins_with("event#"),
            FilterExpression=Attr("detailType").eq(detail_type)
            & Attr("source").eq(source),
        )
    else:
        return get_items_from_dynamodb(
            KeyConditionExpression=Key("testId").eq(test_id)
            & Key("sk").begins_with("event#"),
        )
