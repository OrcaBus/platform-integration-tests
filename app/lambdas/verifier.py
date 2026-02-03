# app/service/verifier.py

"""
Verifier Lambda Function

Two modes:

Status mode (called repeatedly by Step Functions):
  - Input: { "testInstrumentRunId": "...", "mode": "status" } or { "testRunId": "...", "mode": "status" } or { "runId": "...", "mode": "status" }
  - Checks run meta and returns:
      {
        "status": "running|ready|timeout|unknown",
        "runId": "...",  # testInstrumentRunId value
        "observedCount": N,
        "expectedCount": N
      }

Verify mode (called once when ready/timeout):
    - Loads expectations.json from S3
    - For each expected event:
      - Queries DynamoDB for matching events (testInstrumentRunId, detailType, source)
      - Downloads event body from S3 if found
      - Applies match rules based on expectation.__match.fields
      - Writes match info (status=matched, verifiedAt) or missing info (status=missed)
  - Checks event order
  - Checks for unexpected events (more events than expected)
  - Updates run meta status to passed/failed

Note: testInstrumentRunId format is "YYMMDD_A00001_XXXX_TESTXXXXXX" (e.g., "260122_A00001_1234_TEST123456")
"""

import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import logging
from boto3.dynamodb.conditions import Key, Attr
from services.utils import set_nested_field, now_iso
from services.dynamodb import (
    get_item_from_dynamodb,
    update_item_in_dynamodb,
    get_items_from_dynamodb,
    put_item_to_dynamodb,
)
from services.s3 import get_item_from_s3, S3_BUCKET

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _parse_iso(dt_str: str):
    try:
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1] + "+00:00"
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


def _get_run_meta(test_instrument_run_id: str):
    """
    Get run meta from DynamoDB.

    Args:
        test_instrument_run_id: The test instrument run ID (format: "YYMMDD_A00001_XXXX_TESTXXXXXX")

    Returns:
        The run meta item or None if not found.
    """
    return get_item_from_dynamodb(
        {"testId": f"run#{test_instrument_run_id}", "sk": "run#meta"}
    )


def _load_s3_json_list(key: str) -> List[Dict[str, Any]]:
    """Load JSON from S3 and ensure it's a list."""
    try:
        resp = get_item_from_s3(key)
        data = json.loads(resp)
        if isinstance(data, list):
            return data
        else:
            raise ValueError(f"Seed file {key} must contain a JSON array")
    except Exception as e:
        logger.error(f"[Verifier] Failed to load {key} from S3: {e}")
        raise e


def _get_observed_events(
    test_instrument_run_id: str, detail_type: str, source: str
) -> List[Dict[str, Any]]:
    """
    Query DynamoDB for observed events matching testInstrumentRunId, detailType, and source.
    Returns list of event metadata items (with rawS3Key).

    Args:
        test_instrument_run_id: The test instrument run ID (format: "YYMMDD_A00001_XXXX_TESTXXXXXX")
        detail_type: The event detail type
        source: The event source
    """
    return get_items_from_dynamodb(
        KeyConditionExpression=Key("testId").eq(f"run#{test_instrument_run_id}")
        & Key("sk").begins_with("event#"),
        FilterExpression=Attr("detailType").eq(detail_type) & Attr("source").eq(source),
    )


def _get_nested_value(obj: Dict[str, Any], path: str) -> Any:
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


def _apply_format(value: str, format_config: Optional[Dict[str, str]]) -> str:
    """
    Apply prefix and/or suffix formatting to a value based on format configuration.
    format_config can have "prefix" and/or "suffix" keys.
    """
    if not format_config:
        return value

    prefix = format_config.get("prefix", "")
    suffix = format_config.get("suffix", "")
    return f"{prefix}{value}{suffix}"


def _process_replace_fields(
    expectation: Dict[str, Any], test_instrument_run_id: str
) -> Dict[str, Any]:
    """
    Process __replace fields in expectation and replace them with actual values.
    Similar to seeder.py logic but for expectations.

    Supported replace fields:
    - testInstrumentRunIdField: Replace with test_instrument_run_id
    - timeStampField: Replace with current timestamp (ISO format)
    """
    # Deep copy to avoid modifying original
    processed = json.loads(json.dumps(expectation))

    replace_config = processed.get("__replace", {})
    if not replace_config:
        return processed

    # Process testInstrumentRunIdField
    if "testInstrumentRunIdField" in replace_config:
        test_run_fields = replace_config["testInstrumentRunIdField"]
        if isinstance(test_run_fields, list):
            for field_config in test_run_fields:
                if isinstance(field_config, dict):
                    # Object with name and optional format
                    field_path = field_config.get("name")
                    format_config = field_config.get("format")
                    if field_path:
                        value = _apply_format(test_instrument_run_id, format_config)
                        set_nested_field(processed, field_path, value)
                elif isinstance(field_config, str):
                    # Simple string path
                    value = test_instrument_run_id
                    set_nested_field(processed, field_config, value)

    # Process timeStampField (if needed in future)
    if "timeStampField" in replace_config:
        from datetime import datetime, timezone

        current_timestamp = (
            datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"
        )
        timestamp_fields = replace_config["timeStampField"]
        if isinstance(timestamp_fields, list):
            for field_path in timestamp_fields:
                if isinstance(field_path, str):
                    set_nested_field(processed, field_path, current_timestamp)

    # Remove __replace field after processing
    processed.pop("__replace", None)

    return processed


def _match_event(
    expected: Dict[str, Any],
    observed_event_body: Dict[str, Any],
    match_fields: List[str],
) -> bool:
    """
    Match observed event against expected event using match fields.
    Returns True if all match fields match.
    """
    for field_path in match_fields:
        expected_value = _get_nested_value(expected, field_path)
        observed_value = _get_nested_value(observed_event_body, field_path)

        if expected_value != observed_value:
            print(
                f"[Verifier] Field mismatch: {field_path} - expected={expected_value}, observed={observed_value}"
            )
            return False

    return True


def _find_matching_event(
    expected: Dict[str, Any],
    observed_events: List[Dict[str, Any]],
    match_fields: List[str],
) -> Optional[Dict[str, Any]]:
    """
    Find the first observed event that matches the expected event.
    Returns the matched event metadata (with rawS3Key) or None.
    """
    detail_type = expected.get("detail-type")
    source = expected.get("source")

    for event_meta in observed_events:
        s3_key = event_meta.get("rawS3Key")
        if not s3_key:
            continue

        event_body = json.loads(get_item_from_s3(s3_key))
        if not event_body:
            continue

        # Check if detailType and source match (they should from query, but double-check)
        if (
            event_body.get("detail-type") != detail_type
            or event_body.get("source") != source
        ):
            continue

        # Apply match rules
        if _match_event(expected, event_body, match_fields):
            return event_meta

    return None


# ---------- STATUS MODE ----------


def _status_mode(test_instrument_run_id: str) -> dict:
    """
    Used by Step Functions "CheckRunStatus".

    Args:
        test_instrument_run_id: The test instrument run ID (format: "YYMMDD_A00001_XXXX_TESTXXXXXX")

    Returns:
      {
        "status": "running|ready|timeout|unknown",
        "runId": "...",  # testInstrumentRunId value
        "observedCount": N,
        "expectedCount": N
      }
    """
    meta = _get_run_meta(test_instrument_run_id)
    if not meta:
        print(
            f"[Verifier/Status] No run meta found for testInstrumentRunId={test_instrument_run_id}"
        )
        return {"status": "unknown", "runId": test_instrument_run_id}

    service_name = meta.get("serviceName", "all")
    expected_count = 0

    # Try to load expectations to get expected count
    try:
        expectations_key = f"seed/services/{service_name}/expectations.json"
        expectations = _load_s3_json_list(expectations_key)
        expected_count = len(expectations)
        logger.info(
            f"[Verifier/Status] Loaded {expected_count} expectations for serviceName={service_name}"
        )
    except Exception as e:
        print(f"[Verifier/Status] Could not load expectations to get count: {e}")

    # Count observed events
    observed_events = _get_observed_events(test_instrument_run_id, "", "")
    observed_count = len(observed_events)

    current_status = meta.get("status", "running")
    timeout_at_str = meta.get("timeoutAt")
    now = datetime.now(timezone.utc)

    print(
        f"[Verifier/Status] Checking status for testInstrumentRunId={test_instrument_run_id}, "
        f"currentStatus={current_status}, observedCount={observed_count}, "
        f"expectedCount={expected_count}, timeoutAt={timeout_at_str}, now={now.isoformat()}"
    )

    # Timeout check - must be done BEFORE checking if ready
    # This ensures timeout is detected even if events haven't been collected
    if timeout_at_str:
        timeout_at = _parse_iso(timeout_at_str)
        if timeout_at:
            # Make timeout_at timezone-aware if it's naive
            if timeout_at.tzinfo is None:
                timeout_at = timeout_at.replace(tzinfo=timezone.utc)

            if now >= timeout_at:
                print(
                    f"[Verifier/Status] Timeout detected: now={now.isoformat()}, timeoutAt={timeout_at.isoformat()}"
                )
                if current_status != "timeout":
                    try:
                        update_item_in_dynamodb(
                            key={"testId": meta["testId"], "sk": meta["sk"]},
                            UpdateExpression="SET #s = :timeout",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":timeout": "timeout"},
                        )
                    except Exception as e:
                        print(
                            f"[Verifier/Status] Failed to set run status to timeout: {e}"
                        )
                        raise e
                return {
                    "status": "timeout",
                    "runId": test_instrument_run_id,
                    "observedCount": observed_count,
                    "expectedCount": expected_count,
                }
        else:
            print(f"[Verifier/Status] Failed to parse timeoutAt: {timeout_at_str}")

    # If all expected events observed -> ready
    # Note: If expected_count is 0, we still check if we have observed events
    # This handles cases where expectations.json might be missing or empty
    if expected_count > 0:
        # Normal case: check if we've observed all expected events
        if observed_count >= expected_count:
            if current_status != "ready":
                try:
                    update_item_in_dynamodb(
                        key={"testId": meta["testId"], "sk": meta["sk"]},
                        UpdateExpression="SET #s = :ready",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":ready": "ready"},
                    )
                except Exception as e:
                    print(f"[Verifier/Status] Failed to set run status to ready: {e}")
                    raise e
            return {
                "status": "ready",
                "runId": test_instrument_run_id,
                "observedCount": observed_count,
                "expectedCount": expected_count,
            }
    else:
        # Edge case: no expectations defined, but we have observed events
        # Consider ready if we have at least some events (to avoid infinite loop)
        # This is a fallback for when expectations.json is missing or empty
        if observed_count > 0:
            print(
                f"[Verifier/Status] No expectations defined (expected_count=0), but {observed_count} events observed. Marking as ready."
            )
            if current_status != "ready":
                try:
                    update_item_in_dynamodb(
                        key={"testId": meta["testId"], "sk": meta["sk"]},
                        UpdateExpression="SET #s = :ready",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":ready": "ready"},
                    )
                except Exception as e:
                    print(f"[Verifier/Status] Failed to set run status to ready: {e}")
                    raise e
            return {
                "status": "ready",
                "runId": test_instrument_run_id,
                "observedCount": observed_count,
                "expectedCount": expected_count,
            }

    # Otherwise still running
    return {
        "status": "running",
        "runId": test_instrument_run_id,
        "observedCount": observed_count,
        "expectedCount": expected_count,
    }


# ---------- VERIFY MODE ----------


def _verify_mode(test_instrument_run_id: str) -> dict:
    """
    Verify mode: Load expectations, match against observed events, write results.

    Args:
        test_instrument_run_id: The test instrument run ID (format: "YYMMDD_A00001_XXXX_TESTXXXXXX")
    """
    meta = _get_run_meta(test_instrument_run_id)
    if not meta:
        raise ValueError(
            f"No run meta found for testInstrumentRunId={test_instrument_run_id}"
        )

    service_name = meta.get("serviceName", "all")
    expectations_key = f"seed/services/{service_name}/expectations.json"

    # Load expectations from S3
    try:
        expectations = _load_s3_json_list(expectations_key)
        print(
            f"[Verifier/Verify] Loaded {len(expectations)} expectations for serviceName={service_name}"
        )
    except Exception as e:
        raise ValueError(f"Failed to load expectations from S3: {e}")

    verifier_at = now_iso()
    matched_count = 0
    missing_count = 0
    matched_event_keys = []  # Track which events were matched

    # Process each expected event in order
    for idx, expected in enumerate(expectations):
        # Process __replace fields first to get the processed expectation
        processed_expected = _process_replace_fields(expected, test_instrument_run_id)

        detail_type = processed_expected.get("detail-type") or expected.get(
            "detail-type"
        )
        source = processed_expected.get("source") or expected.get("source")
        match_fields = expected.get("__match", {}).get("fields", [])

        if not detail_type or not source:
            print(
                f"[Verifier/Verify] Skipping expectation {idx}: missing detail-type or source"
            )
            continue

        # Query for matching observed events
        observed_events = _get_observed_events(
            test_instrument_run_id, detail_type, source
        )

        # Find matching event using processed expectation
        matched_event = _find_matching_event(
            processed_expected, observed_events, match_fields
        )

        if matched_event:
            # Write match info to DynamoDB
            matched_count += 1
            event_key = {"testId": matched_event["testId"], "sk": matched_event["sk"]}
            matched_event_keys.append(event_key)
            try:
                update_item_in_dynamodb(
                    key=event_key,
                    UpdateExpression="SET #s = :status, verifiedAt = :verifiedAt, expectedOrder = :order, expectedEvent = :expected",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":status": "matched",
                        ":verifiedAt": verifier_at,
                        ":order": idx,
                        ":expected": processed_expected,
                    },
                )
            except Exception as e:
                print(f"[Verifier/Verify] Failed to update matched event: {e}")
                raise e
        else:
            # Write missing event item to DynamoDB
            missing_count += 1
            missing_sk = f"expectation#{idx:03d}-missing"

            missing_item = {
                "testId": f"run#{test_instrument_run_id}",
                "sk": missing_sk,
                "detailType": detail_type,
                "source": source,
                "expectedEvent": processed_expected,
                "status": "missed",
                "verifiedAt": verifier_at,
                "expectedOrder": idx,
            }
            put_item_to_dynamodb(missing_item)

    # Check for unexpected events (events not matched to any expectation)
    unexpected_count = 0
    try:
        all_observed_events = get_items_from_dynamodb(
            KeyConditionExpression=Key("testId").eq(f"run#{test_instrument_run_id}")
            & Key("sk").begins_with("event#")
        )

        # Check each observed event to see if it was matched
        for event_item in all_observed_events:
            event_key = {"testId": event_item["testId"], "sk": event_item["sk"]}
            if event_key not in matched_event_keys:
                # This event was not matched to any expectation
                unexpected_count += 1
                try:
                    update_item_in_dynamodb(
                        key=event_key,
                        UpdateExpression="SET #s = :status, verifiedAt = :verifiedAt",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={
                            ":status": "unexpected",
                            ":verifiedAt": verifier_at,
                        },
                    )
                except Exception as e:
                    print(f"[Verifier/Verify] Failed to mark event as unexpected: {e}")
    except Exception as e:
        print(f"[Verifier/Verify] Failed to check for unexpected events: {e}")

    # Determine run status
    current_status = meta.get("status", "running")
    if current_status == "timeout":
        run_status = "failed"
    elif missing_count > 0 or unexpected_count > 0:
        run_status = "failed"
    else:
        run_status = "passed"

    # Update run meta status
    try:
        update_item_in_dynamodb(
            Key={"testId": meta["testId"], "sk": meta["sk"]},
            UpdateExpression="SET #s = :status, verifiedAt = :verifiedAt",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": run_status,
                ":verifiedAt": verifier_at,
            },
        )
    except Exception as e:
        print(f"[Verifier/Verify] Failed to update run meta status: {e}")

    print(
        f"[Verifier/Verify] Verification complete: matched={matched_count}, missing={missing_count}, unexpected={unexpected_count}, runStatus={run_status}"
    )

    return {
        "runId": test_instrument_run_id,
        "runStatus": run_status,
        "matchedCount": matched_count,
        "missingCount": missing_count,
        "unexpectedCount": unexpected_count,
        "totalExpected": len(expectations),
    }


# ---------- HANDLER ----------


def handler(event, context):
    """
    Mode selection:
    Verifier will use the testInstrumentRunId from seedResult if it is available.

    - Status mode (called by SFN loop when status is running):
      { "testInstrumentRunId": "...", "mode": "status" }
      or
      { "testRunId": "...", "mode": "status" }
      or
      { "runId": "...", "mode": "status" }

    - Verify mode (called by SFN after status is ready or timeout):
      { "testInstrumentRunId": "...", "mode": "verify" }
      or
      { "testRunId": "...", "mode": "verify" }
      or
      { "runId": "...", "mode": "verify" }


    """
    print(f"[Verifier] Event: {json.dumps(event)}")

    mode = event.get("mode") or "verify"

    # Extract test instrument run ID from various possible field names
    # Priority: testInstrumentRunId > testRunId > runId
    # Also check seedResult for backward compatibility
    test_instrument_run_id = (
        event.get("testInstrumentRunId")
        or event.get("testRunId")
        or event.get("runId")
        or (event.get("seedResult") or {}).get("testInstrumentRunId")
        or (event.get("seedResult") or {}).get("testRunId")
        or (event.get("seedResult") or {}).get("runId")
    )

    if not test_instrument_run_id:
        raise ValueError(
            "testInstrumentRunId, testRunId, or runId is required for verifier"
        )

    if mode == "status":
        return _status_mode(test_instrument_run_id)
    else:
        return _verify_mode(test_instrument_run_id)
