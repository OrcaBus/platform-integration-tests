"""
EventBridge operations for the integration testing service.
Contains functions for interacting with AWS EventBridge.
"""

from typing import Dict, Any
import logging
import os
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# EventBridge configuration
EVENT_BUS_NAME = os.environ["EVENT_BUS_NAME"]
RULE_NAME = os.environ["RULE_NAME"]
events_client = boto3.client("events")


def put_event_to_event_bus(entry: Dict[str, Any]) -> Dict[str, Any]:
    """
    Put event to EventBridge bus.

    Args:
        entry: Event entry dictionary with keys like:
            - EventBusName: The event bus name
            - Source: The event source
            - DetailType: The detail type
            - Detail: JSON string of the detail
            - Time: Optional timestamp

    Returns:
        The response from EventBridge

    Example:
        >>> entry = {
        ...     "EventBusName": "my-bus",
        ...     "Source": "my.source",
        ...     "DetailType": "MyEvent",
        ...     "Detail": json.dumps({"key": "value"})
        ... }
        >>> response = put_event_to_event_bus(entry)
    """
    try:
        return events_client.put_events(Entries=[entry])
    except Exception as e:
        logger.error(f"Failed to put event to EventBus: {e}")
        raise e
