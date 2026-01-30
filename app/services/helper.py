from typing import List, Dict, Optional, Tuple
import logging
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

events_helper = {
    "available_services": [
        "all",
        "sequencerunmanager",
        "workflowrunmanager",
        "bclconvertermanager",
    ],
    "abbreviations": {
        "all": "ALL",
        "sequencerunmanager": "SRM",
        "workflowrunmanager": "WRM",
        "bclconvertermanager": "BCM",
    },
    "instrumentRunIdMapping": {
        "SequenceRunStateChange": "detail.instrumentRunId",
        "SequenceRunSampleSheetChange": "detail.instrumentRunId",
        "SequenceRunLibraryLinkingChange": "detail.instrumentRunId",
    },
}


def get_available_services() -> List[str]:
    return events_helper["available_services"]


def get_service_abbreviation(service_name) -> str:
    return events_helper["abbreviations"][service_name]


def get_instrumentRunIdMapping() -> Dict[str, str]:
    return events_helper["instrumentRunIdMapping"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"


def resolve_service_name(raw_service_name: Optional[str]) -> Tuple[str, str]:
    """
    Normalise the serviceName:
    - None or "all" -> "all"
    - otherwise: lowercased string, used as folder name.
    - return the service name and abbreviation
    """
    if raw_service_name is None or str(raw_service_name).lower() == "all":
        return "all", "ALL"

    if raw_service_name not in get_available_services():
        logger.error(
            f"Service name {raw_service_name} not supported for current integration test."
        )
        raise ValueError(
            f"Service name {raw_service_name} not supported for current integration test."
        )

    return str(raw_service_name).lower(), get_service_abbreviation(raw_service_name)


def get_s3_keys_for_service(service_name: str) -> Tuple[str, str]:
    """
    Return (seeds_key, expectations_key) for a given serviceName.
    Layout:
      seed/services/{serviceName}/seeds.json
      seed/services/{serviceName}/expectations.json
    """
    base_prefix = f"seed/services/{service_name}/"
    return (
        base_prefix + "seeds.json",
        base_prefix + "expectations.json",
    )


def deep_replace_in_dict(obj: Any, old_value: str, new_value: str) -> Any:
    """
    Recursively replace all occurrences of old_value with new_value in a nested dict/list structure.
    """
    if isinstance(obj, dict):
        return {
            k: deep_replace_in_dict(v, old_value, new_value) for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [deep_replace_in_dict(item, old_value, new_value) for item in obj]
    elif isinstance(obj, str):
        return obj.replace(old_value, new_value)
    else:
        return obj


def get_nested_value(obj: Dict[str, Any], path: str) -> Any:
    """
    Get nested value from object using dot notation.
    E.g., "detail.instrumentRunId" -> obj["detail"]["instrumentRunId"]
    """
    parts = path.split(".")
    value = obj
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
        if value is None:
            return None
    return value


def set_nested_field(obj: Dict[str, Any], path: str, value: Any) -> None:
    """
    Set a nested field value using dot-notation path (e.g., "detail.icaEvent.id").
    Creates intermediate dictionaries if they don't exist.
    Handles mapping between icaEvent (in path) and ica-event (actual key).
    """
    parts = path.split(".")
    current = obj

    def find_existing_key(container: Dict[str, Any], key: str) -> str:
        """
        Find existing key, handling hyphenated variants.
        Returns the actual key if found, or the requested key if not found.
        Handles: icaEvent <-> ica-event mapping
        """
        # First check if the exact key exists
        if key in container:
            return key

        # Check for hyphenated variant (e.g., icaEvent -> ica-event)
        # Pattern: camelCase ending with "Event" -> kebab-case ending with "-event"
        if key.endswith("Event") and not key.startswith("-"):
            hyphenated = key.replace("Event", "-event")
            if hyphenated in container:
                return hyphenated

        # Check reverse (e.g., ica-event -> icaEvent)
        if key.endswith("-event"):
            unhyphenated = key.replace("-event", "Event")
            if unhyphenated in container:
                return unhyphenated

        return key

    # Navigate through the path, handling hyphenated keys
    for part in parts[:-1]:
        actual_key = find_existing_key(current, part)
        if actual_key not in current:
            current[actual_key] = {}
        elif not isinstance(current[actual_key], dict):
            current[actual_key] = {}
        current = current[actual_key]

    # Set the final value, using the actual key if it exists
    final_key = find_existing_key(current, parts[-1])
    current[final_key] = value
