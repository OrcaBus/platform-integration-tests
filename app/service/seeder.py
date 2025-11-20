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
from typing import Optional, List, Dict, Any, Tuple
import uuid
from datetime import datetime, timedelta
import time

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
        datetime.now(tz=datetime.timezone.utc)
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
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
    """
    Try to load events/expectations for the requested serviceName.
    If those keys don't exist, fall back to 'all'.
    Returns (events, expectations, effective_service_name).
    """
    requested = service_name
    events_key, expectations_key = _s3_keys_for_service(requested)

    try:
        events = _load_s3_json_list(S3_BUCKET, events_key)
        expectations = _load_s3_json_list(S3_BUCKET, expectations_key)
        logger.info("Loaded seeds for serviceName=%s", requested)
        return events, expectations, requested
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code not in ("NoSuchKey", "NoSuchBucket"):
            logger.error("Error loading seeds for serviceName=%s: %s", requested, e)
            raise

        # fall back to 'all'
        logger.warning(
            "Seed definitions for serviceName=%s not found, falling back to 'all'",
            requested,
        )
        events_key, expectations_key = _s3_keys_for_service("all")
        events = _load_s3_json_list(S3_BUCKET, events_key)
        expectations = _load_s3_json_list(S3_BUCKET, expectations_key)
        return events, expectations, "all"


def _publish_test_events(
    test_run_id: str,
    service_name: str,
    events_definitions: List[Dict[str, Any]],
) -> int:
    """
    Publishes test events to EventBridge sequentially, with a delay between each
    to simulate a real service emitting a sequence of status updates over time.

    events_definitions is expected to be an array of objects like:

    {
      "Source": "orca.integrationtest",
      "DetailType": "WorkflowRunCreated",
      "Detail": { ... arbitrary payload ... }
    }
    """
    if not events_definitions:
        logger.info("No events to publish for serviceName=%s", service_name)
        return 0

    published_count = 0

    for idx, ev in enumerate(events_definitions):
        detail = ev.get("Detail", {})
        # Add tracing fields
        detail.setdefault("testRunId", test_run_id)
        detail.setdefault("serviceName", service_name)
        detail.setdefault("testMode", True)

        entry = {
            "EventBusName": EVENT_BUS_NAME,
            "Source": ev["Source"],
            "DetailType": ev["DetailType"],
            "Detail": json.dumps(detail),
        }

        logger.info(
            "Publishing test event %d/%d for testRunId=%s, serviceName=%s",
            idx + 1,
            len(events_definitions),
            test_run_id,
            service_name,
        )

        resp = events_client.put_events(Entries=[entry])
        failed = resp.get("FailedEntryCount", 0)
        if failed:
            logger.error("Failed to publish test event %d: %s", idx + 1, resp)
            raise RuntimeError("One or more events failed to publish")

        published_count += 1

        # If there are more events to send, wait 10 seconds to simulate
        # a realistic emission interval.
        if idx < len(events_definitions) - 1:
            logger.info("Sleeping 10 seconds before publishing next test event")
            time.sleep(10)

    logger.info(
        "Published %d test events to EventBridge for testRunId=%s, serviceName=%s",
        published_count,
        test_run_id,
        service_name,
    )
    return published_count


def _write_expectations(
    test_run_id: str,
    service_name: str,
    expectations: List[Dict[str, Any]],
) -> int:
    """
    Writes expectation items into DynamoDB.

    Each expectation JSON can be arbitrary; we wrap it with:

      pk = f"run#{testRunId}"
      sk = f"expectation#{<id or index>}"
    """
    if not expectations:
        logger.info("No expectations to write for serviceName=%s", service_name)
        return 0

    count = 0
    with table.batch_writer() as batch:
        for idx, exp in enumerate(expectations, start=1):
            exp_id = exp.get("id") or f"{idx:03d}"

            item = {
                "pk": f"run#{test_run_id}",
                "sk": f"expectation#{exp_id}",
                "testRunId": test_run_id,
                "serviceName": service_name,
                "expected": exp,
            }
            batch.put_item(Item=item)
            count += 1

    logger.info(
        "Wrote %d expectation items to DynamoDB for testRunId=%s, serviceName=%s",
        count,
        test_run_id,
        service_name,
    )
    return count


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
    - Create one slot item per fixture
    - Emit initial seed event to EventBridge (testMode=true, testId=runId)
    """
    print(f"[Seeder] Event: {json.dumps(event)}")

    # You can also derive testRunId from event if you prefer something deterministic
    test_run_id = f"it-{uuid.uuid4()}"
    raw_service_name = event.get("serviceName")
    requested_service_name = _resolve_service_name(raw_service_name)

    logger.info(
        "Starting seeding for testRunId=%s, requestedServiceName=%s (raw=%r)",
        test_run_id,
        requested_service_name,
        raw_service_name,
    )

    try:
        events_defs, expectation_defs, effective_service_name = (
            _load_service_seed_definitions(requested_service_name)
        )
    except ClientError as e:
        logger.error(
            "Error loading seed definitions for serviceName=%s: %s",
            requested_service_name,
            e,
        )
        raise

    published_count = _publish_test_events(
        test_run_id, effective_service_name, events_defs
    )
    expectations_count = _write_expectations(
        test_run_id, effective_service_name, expectation_defs
    )

    now = datetime.now(tz=datetime.timezone.utc)
    started_at = _now_iso()
    timeout_at = (now + timedelta(minutes=15)).isoformat(timespec="seconds") + "Z"

    # 2. Create run meta item
    meta_item = {
        "pk": f"run#{test_run_id}",
        "sk": "run#meta",
        "runId": test_run_id,
        "serviceName": effective_service_name,
        "expectedSlots": expectations_count,
        "observedCount": 0,
        "status": "running",
        "startedAt": started_at,
        "timeoutAt": timeout_at,
    }
    table.put_item(Item=meta_item)
    print(f"[Seeder] Created run meta for {test_run_id}")

    return {
        "testRunId": test_run_id,
        "serviceName": effective_service_name,
        "expectedSlots": expectations_count,
        "startedAt": started_at,
        "timeoutAt": timeout_at,
    }
