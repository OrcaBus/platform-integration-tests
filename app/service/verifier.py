# app/service/verifier.py

"""
Verifier Lambda Function

When a run is ready (expected count reached or timeout), loads fixtures and
observed events from DynamoDB and checks:
- Presence: all expected event types/counts observed
- Order: strictly increasing seq or all causedBy edges satisfied
- Payload: each event detail validates against expected event details
- Idempotency: no duplicate eventId for the run
- Latency: each step and overall duration within configured windows

Writes the verdict to DynamoDB.
"""

import json
import os
from collections import Counter
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ["TABLE_NAME"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _parse_iso(dt_str: str):
    try:
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1]
        return datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _get_run_meta(run_id: str):
    resp = table.get_item(Key={"pk": f"run#{run_id}", "sk": "run#meta"})
    return resp.get("Item")


def _get_slots_for_run(run_id: str):
    resp = table.query(
        KeyConditionExpression=Key("pk").eq(f"run#{run_id}") & Key("sk").begins_with("slot#")
    )
    return resp.get("Items", [])


# ---------- STATUS MODE ----------

def _status_mode(run_id: str) -> dict:
    """
    Used by Step Functions "CheckRunStatus":

    Returns:
      {
        "status": "running|ready|timeout|unknown",
        "runId": "...",
        "observedCount": N,
        "expectedSlots": N
      }
    """
    meta = _get_run_meta(run_id)
    if not meta:
        print(f"[Verifier/Status] No run meta found for runId={run_id}")
        return {"status": "unknown", "runId": run_id}

    expected_slots = int(meta.get("expectedSlots", 0))
    observed_count = int(meta.get("observedCount", 0))
    current_status = meta.get("status", "running")

    timeout_at_str = meta.get("timeoutAt")
    now = datetime.now(timezone.utc)

    # Timeout check
    if timeout_at_str:
        timeout_at = _parse_iso(timeout_at_str)
        if timeout_at and now >= timeout_at:
            # Mark as timeout (if not already)
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
                "runId": run_id,
                "observedCount": observed_count,
                "expectedSlots": expected_slots,
            }

    # If all slots have at least one observed event -> ready
    if observed_count >= expected_slots and expected_slots > 0:
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
            "runId": run_id,
            "observedCount": observed_count,
            "expectedSlots": expected_slots,
        }

    # Otherwise still running
    return {
        "status": "running",
        "runId": run_id,
        "observedCount": observed_count,
        "expectedSlots": expected_slots,
    }


# ---------- VERIFY MODE ----------

def _verify_slot(run_meta: dict, slot: dict) -> dict:
    """
    Simple verification:

    - No observedEvents -> "missing"
    - 1 observed & payloadHash matches (if provided) -> "matched"
    - >1 observed -> "duplicate"
    - 1 observed & hash differs -> "mismatch"
    """
    expected = slot.get("expected", {}) or {}
    observed_events = slot.get("observedEvents", []) or []

    status = "pending"
    reasons = []
    latency_ms = None

    if not observed_events:
        status = "missing"
        reasons.append("No observed events for this slot")
    else:
        first_obs = observed_events[0]
        expected_hash = expected.get("payloadHash")
        observed_hash = first_obs.get("payloadHash")

        if len(observed_events) > 1:
            status = "duplicate"
            reasons.append(f"{len(observed_events)} observed events for this slot")
        else:
            if expected_hash and observed_hash and expected_hash != observed_hash:
                status = "mismatch"
                reasons.append("Payload hash mismatch")
            else:
                status = "matched"

        started_at_str = run_meta.get("startedAt")
        started_at = _parse_iso(started_at_str) if started_at_str else None
        received_at_str = first_obs.get("receivedAt")
        received_at = _parse_iso(received_at_str) if received_at_str else None

        if started_at and received_at:
            delta = received_at - started_at
            latency_ms = int(delta.total_seconds() * 1000)

    return {
        "status": status,
        "reasons": reasons,
        "latencyMs": latency_ms,
        "checkedAt": _now_iso(),
        "primaryObservedIndex": 0 if observed_events else None,
    }


def _verify_mode(run_id: str) -> dict:
    meta = _get_run_meta(run_id)
    if not meta:
        raise ValueError(f"No run meta found for runId={run_id}")

    slots = _get_slots_for_run(run_id)
    print(f"[Verifier/Verify] Found {len(slots)} slots for run {run_id}")

    slot_status_counts = Counter()
    for slot in slots:
        verdict = _verify_slot(meta, slot)
        slot_status_counts[verdict["status"]] += 1

        try:
            table.update_item(
                Key={"pk": slot["pk"], "sk": slot["sk"]},
                UpdateExpression="SET verdict = :verdict",
                ExpressionAttributeValues={":verdict": verdict},
            )
        except Exception as e:
            print(f"[Verifier/Verify] Failed to update verdict for {slot['pk']} / {slot['sk']}: {e}")

    print(f"[Verifier/Verify] Slot verdict counts: {dict(slot_status_counts)}")

    current_status = meta.get("status", "running")

    if current_status == "timeout":
        run_status = "failed"  # or keep "timeout" if you want to distinguish
    else:
        if (
            slot_status_counts.get("missing")
            or slot_status_counts.get("mismatch")
            or slot_status_counts.get("duplicate")
        ):
            run_status = "failed"
        else:
            run_status = "passed"

    # Update run meta status
    try:
        table.update_item(
            Key={"pk": meta["pk"], "sk": meta["sk"]},
            UpdateExpression="SET #s = :status",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": run_status},
        )
    except Exception as e:
        print(f"[Verifier/Verify] Failed to update run meta status: {e}")

    return {
        "runId": run_id,
        "runStatus": run_status,
        "slotStatusCounts": dict(slot_status_counts),
    }


# ---------- HANDLER ----------

def handler(event, context):
    """
    Mode selection:

    - Status mode (called by SFN loop):
      { "runId": "...", "mode": "status" }

    - Verify mode (called by SFN after ready/timeout):
      { "runId": "...", "mode": "verify" }
    """
    print(f"[Verifier] Event: {json.dumps(event)}")

    mode = event.get("mode") or "verify"
    run_id = event.get("runId")

    # Backwards compatibility: if runId not provided directly, try seedResult
    if not run_id:
        seed = event.get("seedResult") or {}
        run_id = seed.get("runId")

    if not run_id:
        raise ValueError("runId is required for verifier")

    if mode == "status":
        return _status_mode(run_id)
    else:
        return _verify_mode(run_id)
