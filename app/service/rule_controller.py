import os
import logging
from typing import Any, Dict

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

events_client = boto3.client("events")

RULE_NAME = os.environ["RULE_NAME"]
EVENT_BUS_NAME = os.environ["EVENT_BUS_NAME"]


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Event shape from Step Functions:

    {
        "action": "enable" | "disable"
    }
    """
    action = event.get("action")
    if action not in ("enable", "disable"):
        raise ValueError(f"Unsupported action: {action!r}")

    logger.info(
        "RuleController: action=%s rule=%s eventBus=%s",
        action,
        RULE_NAME,
        EVENT_BUS_NAME,
    )

    if action == "enable":
        events_client.enable_rule(Name=RULE_NAME, EventBusName=EVENT_BUS_NAME)
    else:
        events_client.disable_rule(Name=RULE_NAME, EventBusName=EVENT_BUS_NAME)

    return {
        "ruleName": RULE_NAME,
        "eventBusName": EVENT_BUS_NAME,
        "action": action,
        "status": "ok",
    }
