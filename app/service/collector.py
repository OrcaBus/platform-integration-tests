# app/service/collector.py
"""
Collector

Triggered by EventBridge rule.

EventBridge sends events that include:

  detail.testMode   (bool, optional but recommended)
  detail.testRunId  (string, required for test runs)

Collector:
  - Ignores events without detail.testRunId (not part of an integration test run).
  - Loads run meta (run#meta) to ensure the run exists.
  - Stores the full EventBridge event into S3 using a time-based path.
  - Writes observed event record to DynamoDB with:
    - pk: run#{testRunId}
    - sk: event#{timestamp}-{eventId}
    - detailType, source, payloadHash, rawS3Key, receivedAt

This keeps Collector lightweight and fast - just raw archival.
No matching logic, no status updates, no knowledge of expectations.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

import boto3

TABLE_NAME = os.environ["TABLE_NAME"]
S3_BUCKET = os.environ["S3_BUCKET"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
s3 = boto3.client("s3")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"


def _hash_payload(payload) -> str:
    try:
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
    except Exception:
        return ""


def _store_event_payload(test_run_id: str, event_id: str, full_event: dict) -> str:
    """
    Store the full EventBridge event in S3 and return the key.

    Path layout (time-based hierarchy):

      events/testruns/{testRunId}/{YYYY}/{MM}/{DD}/{timestamp}-{eventId}.json
    """
    now = datetime.now(timezone.utc)
    yyyy = now.strftime("%Y")
    mm = now.strftime("%m")
    dd = now.strftime("%d")
    ts = now.strftime("%Y-%m-%dT%H-%M-%SZ")

    key = f"events/testruns/{test_run_id}/{yyyy}/{mm}/{dd}/{ts}-{event_id}.json"

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


def _get_run_meta(test_run_id: str):
    resp = table.get_item(Key={"pk": f"run#{test_run_id}", "sk": "run#meta"})
    return resp.get("Item")


def handler(event, context):
    """
    EventBridge event shape (simplified):

      {
        "id": "...",
        "source": "...",
        "detail-type": "...",
        "detail": {
          "testMode": true,
          "testRunId": "<runId>",
          ...
        },
        ...
      }
    """
    print(f"[Collector] EventBridge event: {json.dumps(event)}")

    detail = event.get("detail") or {}

    # Only handle events that belong to a test run
    test_run_id = detail.get("testRunId")
    if not test_run_id:
        print("[Collector] No testRunId in event.detail, ignoring.")
        return {"ignored": True, "reason": "no_testRunId"}

    run_meta = _get_run_meta(test_run_id)
    if not run_meta:
        print(f"[Collector] No run meta found for testRunId={test_run_id}, ignoring.")
        return {"ignored": True, "reason": "no_run_meta", "testRunId": test_run_id}

    event_id = event.get("id", "")
    detail_type = event.get("detail-type", "")
    source = event.get("source", "")

    # Store full payload in S3 first (time-based path)
    s3_key = _store_event_payload(test_run_id, event_id, event)
    payload_hash = _hash_payload(detail)
    received_at = _now_iso()

    # Generate sort key: event#{timestamp}-{eventId}
    # Use microsecond precision for uniqueness
    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%Y%m%dT%H%M%S.%f")[:-3]  # milliseconds
    sk = f"event#{timestamp_str}-{event_id}"

    # Write observed event record to DynamoDB
    event_item = {
        "pk": f"run#{test_run_id}",
        "sk": sk,
        "testRunId": test_run_id,
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
            f"[Collector] Stored event record for testRunId={test_run_id}, "
            f"detailType={detail_type}, source={source}"
        )
    except Exception as e:
        print(f"[Collector] Failed to store event record: {e}")
        return {"testRunId": test_run_id, "stored": False, "error": str(e)}

    return {
        "testRunId": test_run_id,
        "stored": True,
        "eventKey": {"pk": f"run#{test_run_id}", "sk": sk},
    }
