"""
Utility functions for the integration testing service.
Contains helper functions, data manipulation, and common utilities used across the service.
"""

import hashlib
import json
import re
from typing import List, Dict, Optional, Tuple, Any
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from utils.config import AVAILABLE_SERVICES, SERVICE_ABBREVIATIONS, TEST_ID_MAPPING

from services.aws.s3 import load_s3_json

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def utc_timestamp(extend_minutes: int = 0) -> str:
    """
    Get current UTC time in ISO format with 'Z' suffix.
    Using extend_minutes to extend the timestamp by the given number of minutes.
    Storage/API format: 2026-02-05T03:09:00Z (UTC ISO 8601)
    Returns:
        ISO formatted timestamp string (e.g., "2025-11-21T10:00:00Z")
    """
    return (
        (datetime.now(timezone.utc) + timedelta(minutes=extend_minutes))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def local_timestamp(tz_name: str = "Australia/Melbourne") -> str:
    """
    Get current UTC time in ISO format with utc timezone.
    Report/filename format (human-friendly): 05 Feb 2026, 14:09 (UTC+11:00)
    Returns:
        ISO formatted timestamp string (e.g., "05 Feb 2026, 14:09 (UTC+11:00)")
    """
    dt = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))
    offset = dt.strftime("%z")
    offset = offset[:3] + ":" + offset[3:]
    return f"{dt:%d %b %Y, %H:%M} (UTC{offset})"


def parse_iso_safe(timestamp: str):
    """Parse ISO timestamp, return None for empty/invalid."""
    if not timestamp:
        return None
    try:
        if isinstance(timestamp, str) and timestamp.endswith("Z"):
            timestamp = timestamp[:-1] + "+00:00"
        return datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return None


def get_safe_timestamp_filename(dt: datetime) -> str:
    """
    Convert datetime to a filename-safe ISO-ish string:
    2025-11-21T10-15-32Z
    """
    return dt.strftime("%Y-%m-%dT%H-%M-%SZ")


def get_available_services() -> List[str]:
    return AVAILABLE_SERVICES


def get_service_abbreviation(service_name: str) -> str:
    return SERVICE_ABBREVIATIONS[service_name]


def validate_service_name(service_name: str) -> bool:
    """
    Check if the service name is valid and supported for the current integration test.
    """
    return service_name in AVAILABLE_SERVICES


# Alias for lambdas that use check_service_name
check_service_name = validate_service_name


def resolve_service_name(raw_service_name: Optional[str]) -> Tuple[str, str]:
    """
    Normalise the serviceName:
    - None or "all" -> "all"
    - otherwise: lowercased string, used as folder name.
    - return the service name and abbreviation

    Args:
        raw_service_name: The raw service name (can be None)

    Returns:
        Tuple of (normalized_service_name, abbreviation)

    Raises:
        ValueError: If service name is not supported
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


def get_event_test_id(event: dict) -> str:
    """
    Get test id from event.
    """
    detail_type = event.get("detail-type") or event.get("DetailType") or "unknown"
    test_id_path = TEST_ID_MAPPING.get(detail_type, None)
    if not test_id_path:
        logger.error(
            f"[Collector] No test id path found for event detail type: {detail_type}"
        )
        raise ValueError(f"No test id path found for event detail type: {detail_type}")
    return get_nested_value(event, test_id_path) or None


def deep_replace_in_dict(obj: Any, old_value: str, new_value: str) -> Any:
    """
    Recursively replace all occurrences of old_value with new_value in a nested dict/list structure.

    Args:
        obj: The object to process (dict, list, or other)
        old_value: The value to replace
        new_value: The replacement value

    Returns:
        The object with all occurrences replaced
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


def is_valid_nested_path(path: str) -> bool:
    """
    Validate nested path using dot notation.
    Example:
        >>> is_valid_nested_path("detail.instrumentRunId")
        True
        >>> is_valid_nested_path("detail.instrumentRunId.subfield")
        True
        >>> is_valid_nested_path("detail.instrumentRunId.subfield.subsubfield")
        True
    """
    return all(part.strip() for part in path.split("."))


def get_nested_value_by_parts(obj: Dict[str, Any], parts: List[str]) -> Any:
    """
    Get nested value from object using list of parts.
    """
    value = obj
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
        if value is None:
            return None
    return value


def get_nested_value(obj: Dict[str, Any], path: str) -> Any:
    """
    Get nested value from object using dot notation.

    Args:
        obj: The dictionary to search
        path: Dot-notation path (e.g., "detail.instrumentRunId")

    Returns:
        The value at the path, or None if not found

    Example:
        >>> get_nested_value({"detail": {"instrumentRunId": "123"}}, "detail.instrumentRunId")
        "123"
    """
    if not is_valid_nested_path(path):
        logger.error(f"Invalid nested path: {path}")
        raise ValueError(f"Invalid nested path: {path}")
    return get_nested_value_by_parts(obj, path.split("."))


def set_nested_value_by_parts(
    obj: Dict[str, Any], parts: List[str], value: Any
) -> None:
    """
    Set nested value using list of parts.
    """
    current = obj
    for part in parts[:-1]:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            logger.error(f"Invalid nested path: {parts}")
            raise ValueError(f"Invalid nested path: {parts}")
    if isinstance(current, dict):
        current[parts[-1]] = value
    else:
        logger.error(f"Invalid nested path: {parts}")
        raise ValueError(f"Invalid nested path: {parts}")


def set_nested_field(obj: Dict[str, Any], path: str, value: Any) -> None:
    """
    Set a nested field value using dot-notation path (e.g., "detail.icaEvent.id").
    Creates intermediate dictionaries if they don't exist.
    Handles mapping between icaEvent (in path) and ica-event (actual key).

    Args:
        obj: The dictionary to modify
        path: Dot-notation path to the field
        value: The value to set

    Example:
        >>> obj = {}
        >>> set_nested_field(obj, "detail.icaEvent.id", "123")
        >>> obj["detail"]["ica-event"]["id"]
        "123"
    """
    if not is_valid_nested_path(path):
        logger.error(f"Invalid nested path: {path}")
        raise ValueError(f"Invalid nested path: {path}")
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

    set_nested_value_by_parts(obj, parts, value)


def process_expectation_replace_fields(
    expectation: Dict[str, Any], test_id: str
) -> Dict[str, Any]:
    """
    Process __replace fields in expectation with actual values.

    Args:
        expectation: The expectation to process
        test_id: The test ID (with format: "run#YYMMDD_A00001_XXXX_TESTXXXXXX")
    Returns:
        The processed expectation with __replace fields replaced with actual values.
    Example:
        >>> process_expectation_replace_fields(expectation, "260122_A00001_1234_TEST123456")
        {
            "detail-type": "SequenceRunStateChange",
            "source": "orcabus.sequencerunmanager",
            "detail": {"instrumentRunId": "260122_A00001_1234_TEST123456"},
            "__replace": {
                "testIdField": [
                    "detail.instrumentRunId"
                ]
            }
        }
    """
    processed = json.loads(json.dumps(expectation))

    replace_config = processed.get("__replace", {})
    if not replace_config:
        return processed

    # Process testInstrumentRunIdField
    if "testIdField" in replace_config:
        test_id_fields = replace_config["testIdField"]
        if isinstance(test_id_fields, list):
            for field_config in test_id_fields:
                if isinstance(field_config, dict):
                    # Object with name and optional format
                    field_path = field_config.get("name")
                    format_config = field_config.get("format")
                    if field_path:
                        value = apply_format(test_id, format_config)
                        set_nested_field(processed, field_path, value)
                elif isinstance(field_config, str):
                    # Simple string path
                    set_nested_field(processed, field_config, test_id)

    # Remove __replace field after processing
    processed.pop("__replace", None)

    return processed


def apply_format(value: str, format_config: Optional[Dict[str, str]]) -> str:
    """
    Apply prefix and/or suffix formatting to a value based on format configuration.
    format_config can have "prefix" and/or "suffix" keys, or both.
    Example:
        >>> apply_format("value", {"prefix": "pre_", "suffix": "_suf"})
        "pre_value_suf"
        >>> apply_format("value", {"prefix": "pre_"})
        "pre_value"
        >>> apply_format("value", {"suffix": "_suf"})
        "value_suf"
        >>> apply_format("value", None)
        "value"
    """
    if not format_config:
        return value
    prefix = format_config.get("prefix", "")
    suffix = format_config.get("suffix", "")
    return f"{prefix}{value}{suffix}"


def hash_payload(payload: Dict[str, Any]) -> Optional[str]:
    """
    Hash a payload using SHA256.
    """
    try:
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
    except Exception:
        return None


def sanitize_string(input_string):
    return (
        re.sub(r"[^\w]+", "_", input_string.strip()).strip("_")
        if input_string
        else None
    )


def validate_event_match(
    expected: Dict[str, Any], observed: Dict[str, Any], match_fields: List[str]
) -> bool:
    """
    Validate if the observed event matches the expected event using the match fields.
    Returns True if all match fields match.
    """
    for field_path in match_fields:
        expected_value = get_nested_value(expected, field_path)
        observed_value = get_nested_value(observed, field_path)
        if expected_value != observed_value:
            return False
    return True


def execute_event_comparison(
    expected: Dict[str, Any],
    observed_events: List[Dict[str, Any]],
    match_fields: List[str],
) -> Optional[Dict[str, Any]]:
    """
    Find the first observed event that matches the expected event.
    Returns the matched event metadata (with rawS3Key) or None.
    Args:
        expected: The expected event
        observed_events: The list of observed events
        match_fields: The list of match fields
    Returns:
        matched event metadata or None
    Example:
        >>> execute_event_comparison(expected, observed_events, match_fields)
        {
            "rawS3Key": "s3://bucket/key",
            "detail-type": "detail-type",
            "source": "source",
        }
    """
    detail_type = expected.get("detail-type")
    source = expected.get("source")

    for event_meta in observed_events:
        observed_event = event_meta.get("observedEvent")
        if not observed_event:
            continue

        # Check if detailType and source match (they should from query, but double-check)
        if (
            observed_event.get("detail-type") != detail_type
            or observed_event.get("source") != source
        ):
            continue

        # Apply match rules
        if validate_event_match(expected, observed_event, match_fields):
            return event_meta

    return None


# test
# if __name__ == "__main__":
#     print(now_iso_storage())
#     print(now_iso_report())
#     print(now_iso_report("Australia/Melbourne"))
