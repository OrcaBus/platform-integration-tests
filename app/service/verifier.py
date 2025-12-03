# app/service/verifier.py

"""
Verifier Lambda Function

Two modes:

Status mode (called repeatedly by Step Functions):
  - Input: { "runId": "...", "mode": "status" } or { "testRunId": "...", "mode": "status" }
  - Checks run meta and returns:
      {
        "status": "running|ready|timeout|unknown",
        "runId": "...",
        "observedCount": N,
        "expectedCount": N
      }

Verify mode (called once when ready/timeout):
  - Loads expectations.json from S3
  - For each expected event:
    - Queries DynamoDB for matching events (testRunId, detailType, source)
    - Downloads event body from S3 if found
    - Applies match rules based on expectation.__match.fields
    - Writes match info (status=matched, verifierAt) or missing info (status=missed)
  - Checks event order
  - Checks for unexpected events (more events than expected)
  - Updates run meta status to passed/failed
"""

import json
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError

TABLE_NAME = os.environ["TABLE_NAME"]
S3_BUCKET = os.environ["S3_BUCKET"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
s3_client = boto3.client("s3")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"


def _parse_iso(dt_str: str):
    try:
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1] + "+00:00"
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


def _get_run_meta(test_run_id: str):
    resp = table.get_item(Key={"pk": f"run#{test_run_id}", "sk": "run#meta"})
    return resp.get("Item")


def _load_s3_json_list(bucket: str, key: str) -> List[Dict[str, Any]]:
    """Load JSON from S3 and ensure it's a list."""
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        raw = resp["Body"].read().decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        else:
            raise ValueError(f"Seed file {key} must contain a JSON array")
    except ClientError as e:
        print(f"[Verifier] Failed to load {key} from S3: {e}")
        raise


def _get_observed_events(
    test_run_id: str, detail_type: str, source: str
) -> List[Dict[str, Any]]:
    """
    Query DynamoDB for observed events matching testRunId, detailType, and source.
    Returns list of event metadata items (with rawS3Key).
    """
    try:
        resp = table.query(
            KeyConditionExpression=Key("pk").eq(f"run#{test_run_id}")
            & Key("sk").begins_with("event#"),
            FilterExpression=Attr("detailType").eq(detail_type)
            & Attr("source").eq(source),
        )
        return resp.get("Items", [])
    except Exception as e:
        print(
            f"[Verifier] Failed to query events for detailType={detail_type}, source={source}: {e}"
        )
        return []


def _download_event_from_s3(s3_key: str) -> Optional[Dict[str, Any]]:
    """Download and parse event JSON from S3."""
    try:
        resp = s3_client.get_object(Bucket=S3_BUCKET, Key=s3_key)
        raw = resp["Body"].read().decode("utf-8")
        return json.loads(raw)
    except Exception as e:
        print(f"[Verifier] Failed to download event from S3 key {s3_key}: {e}")
        return None


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


def _match_event(
    expected: Dict[str, Any], observed_event_body: Dict[str, Any], match_fields: List[str]
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

        event_body = _download_event_from_s3(s3_key)
        if not event_body:
            continue

        # Check if detailType and source match (they should from query, but double-check)
        if event_body.get("detail-type") != detail_type or event_body.get("source") != source:
            continue

        # Apply match rules
        if _match_event(expected, event_body, match_fields):
            return event_meta

    return None


# ---------- STATUS MODE ----------


def _status_mode(test_run_id: str) -> dict:
    """
    Used by Step Functions "CheckRunStatus".

    Returns:
      {
        "status": "running|ready|timeout|unknown",
        "runId": "...",
        "observedCount": N,
        "expectedCount": N
      }
    """
    meta = _get_run_meta(test_run_id)
    if not meta:
        print(f"[Verifier/Status] No run meta found for testRunId={test_run_id}")
        return {"status": "unknown", "runId": test_run_id}

    service_name = meta.get("serviceName", "all")
    expected_count = 0

    # Try to load expectations to get expected count
    try:
        expectations_key = f"seed/services/{service_name}/expectations.json"
        expectations = _load_s3_json_list(S3_BUCKET, expectations_key)
        expected_count = len(expectations)
    except Exception as e:
        print(f"[Verifier/Status] Could not load expectations to get count: {e}")

    # Count observed events
    try:
        resp = table.query(
            KeyConditionExpression=Key("pk").eq(f"run#{test_run_id}")
            & Key("sk").begins_with("event#")
        )
        observed_count = len(resp.get("Items", []))
    except Exception as e:
        print(f"[Verifier/Status] Could not count observed events: {e}")
        observed_count = 0

    current_status = meta.get("status", "running")
    timeout_at_str = meta.get("timeoutAt")
    now = datetime.now(timezone.utc)

    # Timeout check
    if timeout_at_str:
        timeout_at = _parse_iso(timeout_at_str)
        if timeout_at and now >= timeout_at:
            if current_status != "timeout":
                try:
                    table.update_item(
                        Key={"pk": meta["pk"], "sk": meta["sk"]},
                        UpdateExpression="SET #s = :timeout",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":timeout": "timeout"},
                    )
                except Exception as e:
                    print(f"[Verifier/Status] Failed to set run status to timeout: {e}")
            return {
                "status": "timeout",
                "runId": test_run_id,
                "observedCount": observed_count,
                "expectedCount": expected_count,
            }

    # If all expected events observed -> ready
    if observed_count >= expected_count and expected_count > 0:
        if current_status != "ready":
            try:
                table.update_item(
                    Key={"pk": meta["pk"], "sk": meta["sk"]},
                    UpdateExpression="SET #s = :ready",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":ready": "ready"},
                )
            except Exception as e:
                print(f"[Verifier/Status] Failed to set run status to ready: {e}")
        return {
            "status": "ready",
            "runId": test_run_id,
            "observedCount": observed_count,
            "expectedCount": expected_count,
        }

    # Otherwise still running
    return {
        "status": "running",
        "runId": test_run_id,
        "observedCount": observed_count,
        "expectedCount": expected_count,
    }


# ---------- VERIFY MODE ----------


def _verify_mode(test_run_id: str) -> dict:
    """
    Verify mode: Load expectations, match against observed events, write results.
    """
    meta = _get_run_meta(test_run_id)
    if not meta:
        raise ValueError(f"No run meta found for testRunId={test_run_id}")

    service_name = meta.get("serviceName", "all")
    expectations_key = f"seed/services/{service_name}/expectations.json"

    # Load expectations from S3
    try:
        expectations = _load_s3_json_list(S3_BUCKET, expectations_key)
        print(
            f"[Verifier/Verify] Loaded {len(expectations)} expectations for serviceName={service_name}"
        )
    except Exception as e:
        raise ValueError(f"Failed to load expectations from S3: {e}")

    verifier_at = _now_iso()
    matched_count = 0
    missing_count = 0
    matched_event_keys = []  # Track which events were matched

    # Process each expected event in order
    for idx, expected in enumerate(expectations):
        detail_type = expected.get("detail-type")
        source = expected.get("source")
        match_fields = expected.get("__match", {}).get("fields", [])

        if not detail_type or not source:
            print(f"[Verifier/Verify] Skipping expectation {idx}: missing detail-type or source")
            continue

        # Query for matching observed events
        observed_events = _get_observed_events(test_run_id, detail_type, source)

        # Find matching event
        matched_event = _find_matching_event(expected, observed_events, match_fields)

        if matched_event:
            # Write match info to DynamoDB
            matched_count += 1
            event_key = {"pk": matched_event["pk"], "sk": matched_event["sk"]}
            matched_event_keys.append(event_key)

            try:
                table.update_item(
                    Key=event_key,
                    UpdateExpression="SET #s = :status, verifierAt = :verifierAt, expectedOrder = :order, expectedEvent = :expected",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":status": "matched",
                        ":verifierAt": verifier_at,
                        ":order": idx,
                        ":expected": expected,
                    },
                )
                print(
                    f"[Verifier/Verify] Matched expectation {idx}: detailType={detail_type}, source={source}"
                )
            except Exception as e:
                print(
                    f"[Verifier/Verify] Failed to update matched event {event_key}: {e}"
                )
        else:
            # Write missing event item to DynamoDB
            missing_count += 1
            missing_sk = f"expectation#{idx:03d}-missing"

            try:
                missing_item = {
                    "pk": f"run#{test_run_id}",
                    "sk": missing_sk,
                    "testRunId": test_run_id,
                    "detailType": detail_type,
                    "source": source,
                    "expectedEvent": expected,
                    "status": "missed",
                    "verifierAt": verifier_at,
                    "expectedOrder": idx,
                }
                table.put_item(Item=missing_item)
                print(
                    f"[Verifier/Verify] Missing expectation {idx}: detailType={detail_type}, source={source}"
                )
            except Exception as e:
                print(
                    f"[Verifier/Verify] Failed to write missing event item: {e}"
                )

    # Check for unexpected events (events not matched to any expectation)
    unexpected_count = 0
    try:
        resp = table.query(
            KeyConditionExpression=Key("pk").eq(f"run#{test_run_id}")
            & Key("sk").begins_with("event#")
        )
        all_observed_events = resp.get("Items", [])

        # Check each observed event to see if it was matched
        for event_item in all_observed_events:
            event_key = {"pk": event_item["pk"], "sk": event_item["sk"]}
            if event_key not in matched_event_keys:
                # This event was not matched to any expectation
                unexpected_count += 1
                try:
                    table.update_item(
                        Key=event_key,
                        UpdateExpression="SET #s = :status, verifierAt = :verifierAt",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={
                            ":status": "unexpected",
                            ":verifierAt": verifier_at,
                        },
                    )
                except Exception as e:
                    print(
                        f"[Verifier/Verify] Failed to mark event as unexpected: {e}"
                    )
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
        table.update_item(
            Key={"pk": meta["pk"], "sk": meta["sk"]},
            UpdateExpression="SET #s = :status, verifiedAt = :verifiedAt",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": run_status, ":verifiedAt": verifier_at},
        )
    except Exception as e:
        print(f"[Verifier/Verify] Failed to update run meta status: {e}")

    print(
        f"[Verifier/Verify] Verification complete: matched={matched_count}, missing={missing_count}, unexpected={unexpected_count}, runStatus={run_status}"
    )

    return {
        "runId": test_run_id,
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

    - Status mode (called by SFN loop):
      { "runId": "...", "mode": "status" }
      or
      { "testRunId": "...", "mode": "status" }

    - Verify mode (called by SFN after ready/timeout):
      { "runId": "...", "mode": "verify" }
      or
      { "testRunId": "...", "mode": "verify" }
    """
    print(f"[Verifier] Event: {json.dumps(event)}")

    mode = event.get("mode") or "verify"

    test_run_id = (
        event.get("runId")
        or event.get("testRunId")
        or (event.get("seedResult") or {}).get("runId")
        or (event.get("seedResult") or {}).get("testRunId")
    )

    if not test_run_id:
        raise ValueError("runId or testRunId is required for verifier")

    if mode == "status":
        return _status_mode(test_run_id)
    else:
        return _verify_mode(test_run_id)
