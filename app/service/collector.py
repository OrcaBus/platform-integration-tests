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
from typing import Optional

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
    resp = table.get_item(Key={"testId": f"run#{test_run_id}", "sk": "run#meta"})
    return resp.get("Item")


def _extract_instrument_run_id(obj: dict) -> Optional[str]:
    """
    Recursively search for instrumentRunId in nested dictionaries.
    Returns the first instrumentRunId found, or None if not found.
    """
    if not isinstance(obj, dict):
        return None

    # Check if instrumentRunId exists at this level
    if "instrumentRunId" in obj:
        return obj["instrumentRunId"]

    # Recursively search nested dictionaries
    for value in obj.values():
        if isinstance(value, dict):
            result = _extract_instrument_run_id(value)
            if result is not None:
                return result
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    result = _extract_instrument_run_id(item)
                    if result is not None:
                        return result

    return None


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
    Example EventBridge event:
    {
    "version": "0",
    "id": "437de356-417b-82d6-3dec-15c85699c743",
    "detail-type": "SequenceRunStateChange",
    "source": "orcabus.sequencerunmanager",
    "account": "455634345446",
    "time": "2025-12-04T23:19:23Z",
    "region": "ap-southeast-2",
    "resources": [],
    "detail": {
        "id": "seq.01KBNTKGADBP60RAXD241TATDT",
        "instrumentRunId": "251125_A01052_0001_IT001",
        "runVolumeName": "bssh.testvolume.it001",
        "runFolderPath": "",
        "runDataUri": "gds://bssh.testvolume.it001",
        "sampleSheetName": "sampleSheet_it_test.csv",
        "startTime": "2025-11-25T01:59:30Z",
        "endTime": null,
        "status": "STARTED"
        ......
        }
    }
    """
    print(f"[Collector] EventBridge event: {json.dumps(event)}")

    detail = event.get("detail") or {}

    # Only handle events that belong to a test run
    test_run_id = detail.get("testRunId")
    if not test_run_id:
        print("[Collector] No testRunId in event.detail, ignoring.")
        return {"ignored": True, "reason": "no_testRunId"}

    # Extract instrumentRunId from event detail (may be nested)
    instrument_run_id = _extract_instrument_run_id(detail)

    # Validate that instrumentRunId matches testRunId
    if instrument_run_id and instrument_run_id != test_run_id:
        print(
            f"[Collector] instrumentRunId mismatch: instrumentRunId={instrument_run_id}, "
            f"testRunId={test_run_id}, ignoring."
        )
        return {
            "ignored": True,
            "reason": "instrumentRunId_mismatch",
            "testRunId": test_run_id,
            "instrumentRunId": instrument_run_id,
        }

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
        "testId": f"run#{test_run_id}",
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
        "eventKey": {"testId": f"run#{test_run_id}", "sk": sk},
    }
