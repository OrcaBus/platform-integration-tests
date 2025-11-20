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
        "expectedSlots": N
      }

Verify mode (called once when ready/timeout):
  - Input: { "runId": "...", "mode": "verify" } or { "testRunId": "...", "mode": "verify" }
  - Loads run meta + expectation items from DynamoDB and checks:
      - Presence: expectations with no observedEvents -> "missing"
      - Duplicates: >1 observedEvents -> "duplicate"
      - Payload hash match/mismatch
      - Latency: from run.meta.startedAt to first observed receivedAt
  - Writes verdict per expectation item and updates run meta status to passed/failed.
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


# ---------- STATUS MODE ----------


def _status_mode(test_run_id: str) -> dict:
    """
    Used by Step Functions "CheckRunStatus".

    Returns:
      {
        "status": "running|ready|timeout|unknown",
        "runId": "...",
        "observedCount": N,
        "expectedSlots": N
      }
    """
    meta = _get_run_meta(test_run_id)
    if not meta:
        print(f"[Verifier/Status] No run meta found for testRunId={test_run_id}")
        return {"status": "unknown", "runId": test_run_id}

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
                "runId": test_run_id,
                "observedCount": observed_count,
                "expectedSlots": expected_slots,
            }

    # If all expectations have at least one observed event -> ready
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
            "runId": test_run_id,
            "observedCount": observed_count,
            "expectedSlots": expected_slots,
        }

    # Otherwise still running
    return {
        "status": "running",
        "runId": test_run_id,
        "observedCount": observed_count,
        "expectedSlots": expected_slots,
    }


# ---------- VERIFY MODE ----------


def _verify_expectation(run_meta: dict, expectation_item: dict) -> dict:
    """
    Simple verification per expectation:

    - No observedEvents -> "missing"
    - 1 observed & payloadHash matches (if provided) -> "matched"
    - >1 observed -> "duplicate"
    - 1 observed & hash differs -> "mismatch"
    """
    expected = expectation_item.get("expected", {}) or {}
    observed_events = expectation_item.get("observedEvents", []) or []

    status = "pending"
    reasons = []
    latency_ms = None

    if not observed_events:
        status = "missing"
        reasons.append("No observed events for this expectation")
    else:
        first_obs = observed_events[0]
        expected_hash = expected.get("payloadHash")
        observed_hash = first_obs.get("payloadHash")

        if len(observed_events) > 1:
            status = "duplicate"
            reasons.append(
                f"{len(observed_events)} observed events for this expectation"
            )
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


def _verify_mode(test_run_id: str) -> dict:
    meta = _get_run_meta(test_run_id)
    if not meta:
        raise ValueError(f"No run meta found for testRunId={test_run_id}")

    expectations = _get_expectations_for_run(test_run_id)
    print(
        f"[Verifier/Verify] Found {len(expectations)} expectations for run {test_run_id}"
    )

    status_counts = Counter()
    for exp_item in expectations:
        verdict = _verify_expectation(meta, exp_item)
        status_counts[verdict["status"]] += 1

        try:
            table.update_item(
                Key={"pk": exp_item["pk"], "sk": exp_item["sk"]},
                UpdateExpression="SET verdict = :verdict",
                ExpressionAttributeValues={":verdict": verdict},
            )
        except Exception as e:
            print(
                f"[Verifier/Verify] Failed to update verdict for {exp_item['pk']} / {exp_item['sk']}: {e}"
            )

    print(f"[Verifier/Verify] Expectation verdict counts: {dict(status_counts)}")

    current_status = meta.get("status", "running")

    if current_status == "timeout":
        run_status = "failed"  # or keep "timeout" if you want to distinguish
    else:
        if (
            status_counts.get("missing")
            or status_counts.get("mismatch")
            or status_counts.get("duplicate")
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
        "runId": test_run_id,
        "runStatus": run_status,
        "slotStatusCounts": dict(status_counts),
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
