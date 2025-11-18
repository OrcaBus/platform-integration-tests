# app/service/collector.py
"""
Collector

Event mode (triggered by EventBridge rule):
   - EventBridge sends events with detail.testMode = true.
   - Collector:
     - Maps event to a slot (naive matching by detailType for now).
     - Appends an entry to observedEvents.
     - If first observed event for that slot, increments observedCount on run meta.
     - Optionally stores full event payload in S3.

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
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _store_event_payload(run_id: str, event_id: str, full_event: dict) -> str:
    """
    Store the full EventBridge event in S3 and return the key.
    """
    key = f"events/{run_id}/{event_id}.json"
    try:
        s3.put_object(Bucket=S3_BUCKET, Key=key, Body=json.dumps(full_event).encode("utf-8"))
        return key
    except Exception as e:
        print(f"[Collector] Failed to store event payload to S3: {e}")
        return ""


def _get_run_meta(run_id: str):
    resp = table.get_item(Key={"pk": f"run#{run_id}", "sk": "run#meta"})
    return resp.get("Item")


def _get_slots_for_run(run_id: str):
    resp = table.query(
        KeyConditionExpression=Key("pk").eq(f"run#{run_id}") & Key("sk").begins_with("slot#")
    )
    return resp.get("Items", [])


def _find_slot_for_event(run_id: str, detail_type: str):
    """
    Naive mapping: find the first slot whose expected.detailType matches
    and that still has zero observedEvents. Replace with your own logic later.
    """
    slots = _get_slots_for_run(run_id)
    chosen = None
    for slot in slots:
        expected = slot.get("expected", {}) or {}
        if expected.get("detailType") == detail_type:
            if not slot.get("observedEvents"):
                return slot
            if chosen is None:
                chosen = slot
    return chosen


def handler(event, context):
    """
    This Lambda is triggered by EventBridge rule with testMode=true.

    EventBridge event shape:
    {
      "id": "...",
      "source": "...",
      "detail-type": "...",
      "detail": {
        "testMode": true,
        "testId": "<runId>",
        ...
      },
      ...
    }
    """
    print(f"[Collector] EventBridge event: {json.dumps(event)}")

    detail = event.get("detail") or {}
    run_id = detail.get("testId")
    if not run_id:
        print("[Collector] No testId in event.detail, ignoring.")
        return {"ignored": True, "reason": "no_testId"}

    run_meta = _get_run_meta(run_id)
    if not run_meta:
        print(f"[Collector] No run meta found for runId={run_id}, ignoring event.")
        return {"ignored": True, "reason": "no_run_meta", "runId": run_id}

    event_id = event.get("id", "")
    detail_type = event.get("detail-type", "")

    # Store full payload in S3 (optional)
    s3_key = _store_event_payload(run_id, event_id, event)
    payload_hash = _hash_payload(detail)

    # Find a slot to attach this event to
    slot = _find_slot_for_event(run_id, detail_type)
    if not slot:
        print(f"[Collector] No matching slot found for runId={run_id}, detailType={detail_type}")
        return {
            "runId": run_id,
            "attached": False,
            "reason": "no_matching_slot",
        }

    slot_pk = slot["pk"]
    slot_sk = slot["sk"]
    observed_events = slot.get("observedEvents", [])
    is_first_for_slot = len(observed_events) == 0

    new_observed = {
        "eventId": event_id,
        "detailType": detail_type,
        "receivedAt": _now_iso(),
        "payloadHash": payload_hash or None,
        "rawS3Key": s3_key or None,
        "matchReason": "detailType",
    }

    # Update slot: append to observedEvents
    try:
        table.update_item(
            Key={"pk": slot_pk, "sk": slot_sk},
            UpdateExpression="SET observedEvents = list_append(if_not_exists(observedEvents, :empty), :new)",
            ExpressionAttributeValues={
                ":empty": [],
                ":new": [new_observed],
            },
        )
        print(f"[Collector] Appended observed event to {slot_pk} / {slot_sk}")
    except Exception as e:
        print(f"[Collector] Failed to update slot item: {e}")
        return {"runId": run_id, "attached": False, "error": str(e)}

    # If first event for this slot, increment observedCount on run meta
    if is_first_for_slot:
        try:
            table.update_item(
                Key={"pk": f"run#{run_id}", "sk": "run#meta"},
                UpdateExpression="SET observedCount = if_not_exists(observedCount, :zero) + :one",
                ExpressionAttributeValues={":zero": 0, ":one": 1},
            )
            print(f"[Collector] Incremented observedCount for runId={run_id}")
        except Exception as e:
            print(f"[Collector] Failed to increment observedCount: {e}")

    return {
        "runId": run_id,
        "slotKey": {"pk": slot_pk, "sk": slot_sk},
        "attached": True,
    }
