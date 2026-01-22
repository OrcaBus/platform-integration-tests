# app/service/collector.py
"""
Collector

Triggered by EventBridge rule.

EventBridge sends events that include:

  detail.ica-event.instrumentRunId  (string, format: "YYMMDD_A00001_XXXX_TESTXXXXXX" - required for test runs)

Collector:
  - Extracts test_instrument_run_id from detail.ica-event.instrumentRunId.
  - Ignores events without detail.ica-event.instrumentRunId (not part of an integration test run).
  - Loads run meta (run#meta) to ensure the run exists.
  - Stores the full EventBridge event into S3 using a time-based path.
  - Writes observed event record to DynamoDB with:
    - pk: run#{testInstrumentRunId}
    - sk: event#{timestamp}-{eventId}
    - detailType, source, payloadHash, rawS3Key, receivedAt

This keeps Collector lightweight and fast - just raw archival.
No matching logic, no status updates, no knowledge of expectations.
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Optional

import boto3
import logging
import uuid

TABLE_NAME = os.environ["TABLE_NAME"]
S3_BUCKET = os.environ["S3_BUCKET"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
s3 = boto3.client("s3")

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"


def _hash_payload(payload) -> str:
    try:
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
    except Exception:
        return ""


def _store_event_payload(
    test_instrument_run_id: str, event_id: str, full_event: dict
) -> str:
    """
    Store the full EventBridge event in S3 and return the key.

    Path layout (time-based hierarchy):

      events/testruns/{testInstrumentRunId}/{YYYY}/{MM}/{DD}/{timestamp}-{eventId}.json
    """
    now = datetime.now(timezone.utc)
    yyyy = now.strftime("%Y")
    mm = now.strftime("%m")
    dd = now.strftime("%d")

    key = f"events/testruns/{test_instrument_run_id}/{yyyy}/{mm}/{dd}/{event_id}.json"

    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=json.dumps(full_event).encode("utf-8"),
        )
        return key
    except Exception as e:
        print(f"[Collector] Failed to store event payload to S3: {e}")
        return ""


def _get_run_meta(test_instrument_run_id: str):
    """
    Get run meta from DynamoDB.

    Returns the run meta item or None if not found.
    """
    test_id = f"run#{test_instrument_run_id}"
    key = {"testId": test_id, "sk": "run#meta"}

    try:
        logger.info(f"[Collector] Querying DynamoDB for testId={test_id}, sk=run#meta")
        resp = table.get_item(Key=key)
        item = resp.get("Item")

        if item:
            print(
                f"[Collector] Found run meta for testInstrumentRunId={test_instrument_run_id}"
            )
        else:
            print(
                f"[Collector] No item found in DynamoDB response for testId={test_id}, sk=run#meta"
            )
            # Log the full response for debugging
            logger.info(f"[Collector] DynamoDB response: {json.dumps(resp)}")

        return item
    except Exception as e:
        logger.error(
            f"[Collector] Error querying DynamoDB for testInstrumentRunId={test_instrument_run_id}: {e}"
        )
        logger.error(f"[Collector] Query key was: {json.dumps(key)}")
        return None


def handler(event, context):
    """
    Collector  will consume all events from the EventBridge bus.
    EventBridge event shape (simplified):
      {
        "id": "...",
        "source": "...",
        "detail-type": "...",
        "detail": {
          "instrumentRunId": "YYMMDD_A00001_XXXX_TESTXXXXXX",
          ...
        },
        ...
    }
    Example EventBridge event:
    {
    "version": "0",
    "id": "7fff8d7d-fed8-b38f-2c0b-c843b0e194e2",
    "detail-type": "Event from aws:sqs",
    "source": "Pipe IcaEventPipeConstru-IntegrationTest",
    "account": "455634345446",
    "time": "2026-01-22T02:11:48Z",
    "region": "ap-southeast-2",
    "resources": [],
    "detail": {
        "instrumentRunId": "260122_A00001_1234_TEST123456",
        ...
    }
    """
    logger.info(f"[Collector] EventBridge event: {json.dumps(event)}")
    # Check if it is a event from details.detail-type: "Event from aws:sqs", if yes, ignore the event as it is seed events
    # if no, continue with the event

    if event.get("detail-type") == "Event from aws:sqs":
        logger.info("[Collector] Event is a seed event, ignoring.")
        return {"ignored": True, "reason": "seed_event"}

    test_instrument_run_id = event.get("detail").get("instrumentRunId")
    run_meta = _get_run_meta(test_instrument_run_id)
    if not run_meta:
        logger.info(
            f"[Collector] No run meta found for testInstrumentRunId={test_instrument_run_id}, ignoring event."
        )
        return {
            "ignored": True,
            "reason": "no_run_meta",
            "testInstrumentRunId": test_instrument_run_id,
        }
    logger.info(
        f"[Collector] Successfully found run meta for testInstrumentRunId={test_instrument_run_id}"
    )

    event_id = (
        event.get("detail").get("id")
        if event.get("detail").get("id")
        else uuid.uuid4().replace("-", "")
    )
    detail_type = event.get("detail-type", "")
    source = event.get("source", "")

    # Store full payload in S3 first (time-based path)
    s3_key = _store_event_payload(test_instrument_run_id, event_id, event)
    payload_hash = _hash_payload(event.get("detail"))
    received_at = _now_iso()

    # Generate sort key: event#{timestamp}-{eventId}
    # Use microsecond precision for uniqueness
    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%Y%m%dT%H%M%S.%f")[:-3]  # milliseconds
    sk = f"event#{timestamp_str}"

    # Write observed event record to DynamoDB
    event_item = {
        "testId": f"run#{test_instrument_run_id}",
        "sk": sk,
        "eventId": event_id,
        "detailType": detail_type,
        "source": source,
        "payloadHash": payload_hash or None,
        "rawS3Key": s3_key or None,
        "receivedAt": received_at,
    }

    try:
        table.put_item(Item=event_item)
        print(
            f"[Collector] Stored event record for testInstrumentRunId={test_instrument_run_id}, "
            f"detailType={detail_type}, source={source}"
        )
    except Exception as e:
        print(f"[Collector] Failed to store event record: {e}")
        return {
            "testInstrumentRunId": test_instrument_run_id,
            "stored": False,
            "error": str(e),
        }

    return {
        "testInstrumentRunId": test_instrument_run_id,
        "stored": True,
        "eventKey": {"testId": f"run#{test_instrument_run_id}", "sk": sk},
    }
