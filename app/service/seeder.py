# app/service/seeder.py
"""
Seeder Lambda Function

- Create run#meta item
- Create one slot item per fixture
- Emit initial seed event to EventBridge (testMode=True, testId=runId)
"""

import os
import json
import logging
import re
from typing import Optional, List, Dict, Any, Tuple
import uuid
from datetime import datetime, timedelta, timezone
import time
import random
import boto3
from botocore.exceptions import ClientError

TABLE_NAME = os.environ["TABLE_NAME"]
EVENT_BUS_NAME = os.environ["EVENT_BUS_NAME"]
S3_BUCKET = os.environ["S3_BUCKET"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
events_client = boto3.client("events")
s3_client = boto3.client("s3")

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _now_iso() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _resolve_service_name(raw_service_name: Optional[str]) -> str:
    """
    Normalise the serviceName:
    - None or "all" -> "all"
    - otherwise: lowercased string, used as folder name.
    """
    if raw_service_name is None or str(raw_service_name).lower() == "all":
        return "all"
    return str(raw_service_name).lower()


def _s3_keys_for_service(service_name: str) -> Tuple[str, str]:
    """
    Return (events_key, expectations_key) for a given serviceName.
    Layout:
      seed/services/{serviceName}/events.json
      seed/services/{serviceName}/expectations.json
    """
    base_prefix = f"seed/services/{service_name}/"
    return (
        base_prefix + "events.json",
        base_prefix + "expectations.json",
    )


def _load_s3_json_list(bucket: str, key: str) -> List[Dict[str, Any]]:
    """
    Load JSON from S3 and ensure it's a list.
    If the object does not exist, raise ClientError with NoSuchKey.
    """
    logger.info("Loading seed data from s3://%s/%s", bucket, key)
    resp = s3_client.get_object(Bucket=bucket, Key=key)
    raw = resp["Body"].read().decode("utf-8")
    data = json.loads(raw)

    if isinstance(data, list):
        return data
    else:
        logger.error("Expected a JSON array in %s but got %s", key, type(data))
        raise ValueError(f"Seed file {key} must contain a JSON array")


def _load_service_seed_definitions(
    service_name: str,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Try to load events for the requested serviceName.
    If those keys don't exist, fall back to 'all'.
    Returns (events, effective_service_name).
    """
    requested_service_name = service_name
    events_key, _ = _s3_keys_for_service(requested_service_name)

    try:
        events = _load_s3_json_list(S3_BUCKET, events_key)
        logger.info("Loaded seeds for serviceName=%s", requested_service_name)
        return events, requested_service_name
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code not in ("NoSuchKey", "NoSuchBucket"):
            logger.error(
                "Error loading seeds for serviceName=%s: %s", requested_service_name, e
            )
            raise

        # fall back to 'all'
        logger.warning(
            "Seed definitions for serviceName=%s not found, falling back to 'all'",
            requested_service_name,
        )
        events_key, _ = _s3_keys_for_service("all")
        events = _load_s3_json_list(S3_BUCKET, events_key)
        return events, "all"


def _deep_replace_in_dict(obj: Any, old_value: str, new_value: str) -> Any:
    """
    Recursively replace all occurrences of old_value with new_value in a nested dict/list structure.
    """
    if isinstance(obj, dict):
        return {
            k: _deep_replace_in_dict(v, old_value, new_value) for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [_deep_replace_in_dict(item, old_value, new_value) for item in obj]
    elif isinstance(obj, str):
        return obj.replace(old_value, new_value)
    else:
        return obj


def _publish_test_events(
    instrument_run_id: str,
    service_name: str,
    events_definitions: List[Dict[str, Any]],
) -> int:
    """
    Publishes test events to EventBridge sequentially, with a delay between each
    to simulate a real service emitting a sequence of status updates over time.

    events_definitions is expected to be an array of EventBridge event objects like:

    {
      "source": "Pipe IcaEventPipeConstru-IntegrationTest",
      "detail-type": "Event from aws:sqs",
      "detail": { ... arbitrary payload ... }
    }

    Supports both lowercase (new format) and capitalized (legacy) field names.

    Dynamically replaces:
    - instrumentRunId and name with test_instrument_run_id format
    - id with r.{uuid} format (uuid without "-")
    - apiUrl to use the new id, but fake url
    """
    if not events_definitions:
        logger.info("No events to publish for serviceName=%s", service_name)
        return 0

    # Generate dynamic IDs for this test run
    run_id = f"r.{uuid.uuid4().replace('-', '')}"
    logger.info(
        "Generated dynamic IDs for instrumentRunId=%s, id=%s",
        instrument_run_id,
        run_id,
    )

    published_count = 0

    for idx, ev in enumerate(events_definitions):
        # Extract source and detail-type (handle both lowercase and capitalized)
        source = ev.get("source") or ev.get("Source")
        detail_type = (
            ev.get("detail-type") or ev.get("DetailType") or ev.get("detailType")
        )

        if not source:
            logger.error("Event %d missing 'source' or 'Source' field", idx + 1)
            raise ValueError(f"Event {idx + 1} must have a 'source' field")
        if not detail_type:
            logger.error(
                "Event %d missing 'detail-type' or 'DetailType' field", idx + 1
            )
            raise ValueError(f"Event {idx + 1} must have a 'detail-type' field")

        # Extract detail (handle both lowercase and capitalized)
        detail = ev.get("detail") or ev.get("Detail", {})

        # If detail is not a dict, wrap it or use as-is
        if not isinstance(detail, dict):
            detail = {"data": detail}

        # Deep copy detail to avoid modifying the original
        detail = json.loads(json.dumps(detail))

        # Helper function to replace patterns in strings recursively
        def _replace_patterns_in_dict(obj: Any) -> Any:
            """Recursively replace r.itXXX patterns and instrumentRunId/name patterns."""
            if isinstance(obj, dict):
                result = {}
                for k, v in obj.items():
                    if k in ("id", "instrumentRunId", "name") and isinstance(v, str):
                        # Replace r.itXXX patterns in id fields
                        if k == "id" and re.match(r"r\.it\d+", v):
                            result[k] = run_id
                        # Replace instrumentRunId/name patterns (date-based format)
                        elif k in ("instrumentRunId", "name") and re.match(
                            r"\d{6}_A\d+_\d{4}_TEST\d+", v
                        ):
                            result[k] = instrument_run_id
                        else:
                            result[k] = _replace_patterns_in_dict(v)
                    elif k == "apiUrl" and isinstance(v, str):
                        # Replace /runs/r.itXXX patterns in URLs
                        result[k] = re.sub(r"/runs/r\.it\d+", f"/runs/{run_id}", v)
                    else:
                        result[k] = _replace_patterns_in_dict(v)
                return result
            elif isinstance(obj, list):
                return [_replace_patterns_in_dict(item) for item in obj]
            elif isinstance(obj, str):
                if re.match(r"r\.it\d+", obj):
                    return obj.replace(r"r\.it\d+", run_id)
                if re.match(r"\d{6}_A\d+_\d{4}_TEST\d+", obj):
                    return obj.replace(r"\d{6}_A\d+_\d{4}_TEST\d+", instrument_run_id)
                else:
                    return obj
            else:
                return obj

        # Apply pattern replacements
        detail = _replace_patterns_in_dict(detail)

        # Explicitly update nested structures like detail["ica-event"] if they exist
        # This ensures the fields are set even if they didn't exist before
        if "ica-event" in detail:
            ica_event = detail["ica-event"]
            if isinstance(ica_event, dict):
                ica_event["id"] = run_id
                ica_event["instrumentRunId"] = instrument_run_id
                ica_event["name"] = instrument_run_id
                # Update apiUrl if it exists
                if "apiUrl" in ica_event and isinstance(ica_event["apiUrl"], str):
                    ica_event["apiUrl"] = re.sub(
                        r"/runs/r\.it\d+", f"/runs/{run_id}", ica_event["apiUrl"]
                    )

            detail.setdefault("instrumentRunId", instrument_run_id)

        entry = {
            "EventBusName": EVENT_BUS_NAME,
            "Source": source,
            "DetailType": detail_type,
            "Detail": json.dumps(detail),
        }

        logger.info(
            "Publishing test event %d/%d for instrumentRunId=%s, serviceName=%s (source=%s, detailType=%s)",
            idx + 1,
            len(events_definitions),
            instrument_run_id,
            service_name,
            source,
            detail_type,
        )

        resp = events_client.put_events(Entries=[entry])
        failed = resp.get("FailedEntryCount", 0)
        if failed:
            logger.error("Failed to publish test event %d: %s", idx + 1, resp)
            raise RuntimeError("One or more events failed to publish")

        published_count += 1

        # If there are more events to send, wait 1 second to simulate
        # a realistic emission interval.
        if idx < len(events_definitions) - 1:
            logger.info("Sleeping 1 second before publishing next test event")
            time.sleep(1)

    logger.info(
        "Published %d test events to EventBridge for instrumentRunId=%s, serviceName=%s",
        published_count,
        instrument_run_id,
        service_name,
    )
    return published_count


def _generate_test_instrument_run_id() -> str:
    """
    Generate a test run id (instrumentRunId) in the format
    date in format YYMMDD_A00001_XXXX_TESTXXXXXX
    X will be randomly generated from 0001 to 9999 in string format
    """
    # Generate random number from 0001 to 9999
    random_number_str_4digits = random.randint(1, 9999)
    random_number_str_6digits = random.randint(1, 999999)
    return f"{datetime.now(tz=timezone.utc).strftime('%y%m%d')}_A00001_{random_number_str_4digits:04d}_TEST{random_number_str_6digits:06d}"


def handler(event, context):
    """
    Expected Step Functions input:
    {
      "runId": "<uuid or pipeline-provided>",
      "scenario": "daily-batch-orchestration",
      ... (other fields ignored)
    }

    Seeder will:
    - Create run#meta item
    - Emit seed events to EventBridge (testMode=true, testId=runId)
    """
    print(f"[Seeder] Event: {json.dumps(event)}")

    # we
    test_instrument_run_id = _generate_test_instrument_run_id()
    raw_service_name = event.get("serviceName")
    requested_service_name = _resolve_service_name(raw_service_name)

    logger.info(
        "Starting seeding for testInstrumentRunId=%s, requestedServiceName=%s (raw=%r)",
        test_instrument_run_id,
        requested_service_name,
        raw_service_name,
    )

    try:
        events_defs, effective_service_name = _load_service_seed_definitions(
            requested_service_name
        )
    except ClientError as e:
        logger.error(
            "Error loading seed definitions for serviceName=%s: %s",
            requested_service_name,
            e,
        )
        raise

    now = datetime.now(tz=timezone.utc)
    started_at = _now_iso()
    # Create timeout_at in ISO format ending with Z (UTC)
    timeout_at = (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Create run meta item FIRST to ensure it exists before events are published
    # This prevents the collector from ignoring events due to missing run meta
    meta_item = {
        "testId": f"run#{test_instrument_run_id}",
        "sk": "run#meta",
        "serviceName": effective_service_name,
        "observedCount": 0,
        "status": "running",
        "startedAt": started_at,
        "timeoutAt": timeout_at,
    }
    table.put_item(Item=meta_item)
    logger.info(
        f"[Seeder] Created run meta for testInstrumentRunId={test_instrument_run_id}"
    )

    # 2. Publish test events to EventBridge AFTER meta item is created
    published_count = _publish_test_events(
        test_instrument_run_id, effective_service_name, events_defs
    )

    return {
        "testInstrumentRunId": test_instrument_run_id,
        "serviceName": effective_service_name,
        "startedAt": started_at,
        "timeoutAt": timeout_at,
        "publishedCount": published_count,
    }
