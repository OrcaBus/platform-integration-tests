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
  - Finds a matching expectation item for this event (naive by detailType).
  - Appends an entry to expectation.observedEvents.
  - If this is the first observed event for that expectation, increments
    observedCount on the run meta item.
"""

import hashlib
import json
import os
from datetime import datetime

import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ["TABLE_NAME"]
S3_BUCKET = os.environ["S3_BUCKET"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
s3 = boto3.client("s3")


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


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
    now = datetime.utcnow()
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


def _get_expectations_for_run(test_run_id: str):
    """
    Fetch all expectation items for this run:

      pk = run#{testRunId}
      sk begins_with expectation#
    """
    resp = table.query(
        KeyConditionExpression=Key("pk").eq(f"run#{test_run_id}")
        & Key("sk").begins_with("expectation#")
    )
    return resp.get("Items", [])


def _find_expectation_for_event(test_run_id: str, detail_type: str):
    """
    Naive mapping: find the first expectation whose expected.detailType matches
    and that still has zero observedEvents. If none are empty, return the first match.

    Expectation item shape (written by Seeder):

      {
        "pk": "run#{testRunId}",
        "sk": "expectation#{id}",
        "testRunId": "...",
        "serviceName": "...",
        "expected": {
          "detailType": "WorkflowRunCreated",
          ...
        },
        "observedEvents": [ ... ]  # optional
      }
    """
    expectations = _get_expectations_for_run(test_run_id)
    chosen = None

    for exp_item in expectations:
        expected = exp_item.get("expected", {}) or {}
        if expected.get("detailType") != detail_type:
            continue

        observed = exp_item.get("observedEvents") or []
        if not observed:
            # Prefer expectations that haven't seen any events yet
            return exp_item

        if chosen is None:
            chosen = exp_item

    return chosen


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

    # Optional: further guard by testMode if you set it in Seeder
    if not detail.get("testMode", False):
        print(
            f"[Collector] testMode is not true for testRunId={test_run_id}, ignoring."
        )
        return {
            "ignored": True,
            "reason": "testMode_not_true",
            "testRunId": test_run_id,
        }

    run_meta = _get_run_meta(test_run_id)
    if not run_meta:
        print(f"[Collector] No run meta found for testRunId={test_run_id}, ignoring.")
        return {"ignored": True, "reason": "no_run_meta", "testRunId": test_run_id}

    event_id = event.get("id", "")
    detail_type = event.get("detail-type", "")

    # Store full payload in S3 first (time-based path)
    s3_key = _store_event_payload(test_run_id, event_id, event)
    payload_hash = _hash_payload(detail)

    # Find a matching expectation to attach this observed event to
    expectation_item = _find_expectation_for_event(test_run_id, detail_type)
    if not expectation_item:
        print(
            f"[Collector] No matching expectation found for "
            f"testRunId={test_run_id}, detailType={detail_type}"
        )
        return {
            "testRunId": test_run_id,
            "attached": False,
            "reason": "no_matching_expectation",
        }

    pk = expectation_item["pk"]
    sk = expectation_item["sk"]
    observed_events = expectation_item.get("observedEvents", [])
    is_first_for_expectation = len(observed_events) == 0

    new_observed = {
        "eventId": event_id,
        "detailType": detail_type,
        "receivedAt": _now_iso(),
        "payloadHash": payload_hash or None,
        "rawS3Key": s3_key or None,
        "matchReason": "detailType",
    }

    # Update expectation item: append to observedEvents
    try:
        table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression=(
                "SET observedEvents = list_append(if_not_exists(observedEvents, :empty), :new)"
            ),
            ExpressionAttributeValues={
                ":empty": [],
                ":new": [new_observed],
            },
        )
        print(f"[Collector] Appended observed event to {pk} / {sk}")
    except Exception as e:
        print(f"[Collector] Failed to update expectation item: {e}")
        return {"testRunId": test_run_id, "attached": False, "error": str(e)}

    # If first event for this expectation, increment observedCount on run meta
    if is_first_for_expectation:
        try:
            table.update_item(
                Key={"pk": f"run#{test_run_id}", "sk": "run#meta"},
                UpdateExpression=(
                    "SET observedCount = if_not_exists(observedCount, :zero) + :one"
                ),
                ExpressionAttributeValues={":zero": 0, ":one": 1},
            )
            print(f"[Collector] Incremented observedCount for testRunId={test_run_id}")
        except Exception as e:
            print(f"[Collector] Failed to increment observedCount: {e}")

    return {
        "testRunId": test_run_id,
        "expectationKey": {"pk": pk, "sk": sk},
        "attached": True,
    }
