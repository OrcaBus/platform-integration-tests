# app/service/reporter.py
"""
Reporter Lambda:

- Input (from Step Functions):
  {
    "runId": "...",
    "verifyResult": { ... },  # output of the Verifier
    ...
  }

- Behavior:
  - Loads run meta + all slot items.
  - Generates a simple HTML report.
  - Stores it in S3.
  - Updates run meta with reportS3Key.
  - Returns report key (and basic summary).
"""

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


def _get_run_meta(run_id: str):
    resp = table.get_item(Key={"pk": f"run#{run_id}", "sk": "run#meta"})
    return resp.get("Item")


def _get_slots_for_run(run_id: str):
    resp = table.query(
        KeyConditionExpression=Key("pk").eq(f"run#{run_id}")
        & Key("sk").begins_with("slot#")
    )
    return resp.get("Items", [])


def _build_html_report(run_meta: dict, slots: list, verify_result: dict) -> str:
    run_id = run_meta.get("runId")
    scenario = run_meta.get("scenario")
    status = run_meta.get("status")
    started_at = run_meta.get("startedAt")
    timeout_at = run_meta.get("timeoutAt")

    slot_rows = []
    for slot in sorted(slots, key=lambda s: s.get("sk", "")):
        sk = slot.get("sk")
        slot_type = slot.get("slotType")
        slot_id = slot.get("slotId")
        expected = slot.get("expected", {})
        observed = slot.get("observedEvents", [])
        verdict = slot.get("verdict", {})
        slot_status = verdict.get("status", "pending")

        slot_rows.append(
            f"<tr>"
            f"<td>{sk}</td>"
            f"<td>{slot_type}</td>"
            f"<td>{slot_id}</td>"
            f"<td>{expected.get('detailType','')}</td>"
            f"<td>{len(observed)}</td>"
            f"<td>{slot_status}</td>"
            f"<td>{'; '.join(verdict.get('reasons', []))}</td>"
            f"</tr>"
        )

    slot_table_html = (
        "<table border='1' cellspacing='0' cellpadding='4'>"
        "<thead><tr>"
        "<th>SK</th><th>SlotType</th><th>SlotId</th><th>Expected detailType</th>"
        "<th>#Observed</th><th>Status</th><th>Reasons</th>"
        "</tr></thead>"
        "<tbody>" + "".join(slot_rows) + "</tbody></table>"
    )

    verify_json = json.dumps(verify_result or {}, indent=2)

    html = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>OrcaBus Integration Test Report - {run_id}</title>
  <style>
    body {{ font-family: sans-serif; }}
    h1, h2, h3 {{ font-family: sans-serif; }}
    table {{ border-collapse: collapse; margin-top: 1em; }}
    th, td {{ padding: 4px 8px; }}
    th {{ background: #eee; }}
  </style>
</head>
<body>
  <h1>OrcaBus Integration Test Report</h1>
  <h2>Run: {run_id}</h2>
  <p><b>Scenario:</b> {scenario}</p>
  <p><b>Status:</b> {status}</p>
  <p><b>Started at:</b> {started_at}</p>
  <p><b>Timeout at:</b> {timeout_at}</p>
  <p><b>Generated at:</b> {_now_iso()}</p>

  <h3>Summary</h3>
  <pre>{verify_json}</pre>

  <h3>Slots</h3>
  {slot_table_html}
</body>
</html>
"""
    return html


def handler(event, context):
    print(f"[Reporter] Event: {json.dumps(event)}")

    run_id = event.get("runId")
    if not run_id:
        vr = event.get("verifyResult") or {}
        run_id = vr.get("runId")

    if not run_id:
        raise ValueError("runId is required for Reporter")

    verify_result = event.get("verifyResult") or {}

    run_meta = _get_run_meta(run_id)
    if not run_meta:
        raise ValueError(f"No run meta found for runId={run_id}")

    slots = _get_slots_for_run(run_id)

    # Build HTML
    html = _build_html_report(run_meta, slots, verify_result)

    # Store to S3
    date_prefix = datetime.utcnow().strftime("%Y-%m-%d")
    key = f"reports/{date_prefix}/{run_id}.html"

    s3.put_object(
        Bucket=S3_BUCKET, Key=key, ContentType="text/html", Body=html.encode("utf-8")
    )
    print(f"[Reporter] Stored report at s3://{S3_BUCKET}/{key}")

    # Update run meta with reportS3Key
    try:
        table.update_item(
            Key={"pk": run_meta["pk"], "sk": run_meta["sk"]},
            UpdateExpression="SET reportS3Key = :key",
            ExpressionAttributeValues={":key": key},
        )
    except Exception as e:
        print(f"[Reporter] Failed to update run meta with report key: {e}")

    return {
        "runId": run_id,
        "reportS3Key": key,
        "bucket": S3_BUCKET,
    }
