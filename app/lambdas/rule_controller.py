import logging
from typing import Any, Dict

from services.eventbridge import events_client, RULE_NAME, EVENT_BUS_NAME

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Event shape from Step Functions:

    {
        "action": "enable" | "disable"
        "serviceName": "all" | "serviceName"
    }
    Example:
    {
        "action": "enable",
        "serviceName": "workflowrunmanager"
    }
    """
    logger.info(f"RuleController: event={event}")
    serviceName = event.get("serviceName", "all")
    action = event.get("action")
    if action not in ("enable", "disable"):
        raise ValueError(f"Unsupported action: {action!r}")

    logger.info(
        "RuleController: action=%s rule=%s eventBus=%s",
        action,
        RULE_NAME,
        EVENT_BUS_NAME,
    )

    try:
        if action == "enable":
            events_client.enable_rule(Name=RULE_NAME, EventBusName=EVENT_BUS_NAME)
        else:
            events_client.disable_rule(Name=RULE_NAME, EventBusName=EVENT_BUS_NAME)
    except Exception as e:
        logger.error(f"RuleController: error={e}")
        raise e

    return {
        "ruleName": RULE_NAME,
        "eventBusName": EVENT_BUS_NAME,
        "action": action,
        "status": "ok",
        "serviceName": serviceName,  # passed through from step functions
    }
