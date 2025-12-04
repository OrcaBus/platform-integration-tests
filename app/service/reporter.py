# app/service/reporter.py
"""
Reporter Lambda:

- Input (from Step Functions):
  {
    "testRunId": "...",  # or "runId"
    "serviceName": "...",
    "verifyResult": { ... },  # output of the Verifier
    ...
  }

- Behavior:
  - Loads run meta from DynamoDB.
  - Queries DynamoDB for matched, missing, and unexpected events.
  - Generates a detailed HTML report.
  - Stores it in S3.
  - Updates run meta with reportS3Key.
  - Returns report key (and basic summary).
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ["TABLE_NAME"]
S3_BUCKET = os.environ["S3_BUCKET"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
s3_client = boto3.client("s3")


TEMPLATE_KEY = "reports/templates/base.html"


def _safe_timestamp_filename(dt: datetime) -> str:
    """
    Convert datetime to a filename-safe ISO-ish string:
    2025-11-21T10-15-32Z
    """
    return dt.strftime("%Y-%m-%dT%H-%M-%SZ")


def _load_template() -> str:
    """
    Try to load HTML template from S3.
    If it doesn't exist, return a very simple fallback template.
    """
    try:
        resp = s3_client.get_object(Bucket=S3_BUCKET, Key=TEMPLATE_KEY)
        body = resp["Body"].read().decode("utf-8")
        return body
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code not in ("NoSuchKey", "NoSuchBucket"):
            logger.error("Failed to load report template: %s", e)
            raise

        logger.warning(
            "Template %s not found in bucket %s; using fallback template",
            TEMPLATE_KEY,
            S3_BUCKET,
        )
        # Simple fallback template with placeholders
        return """
        <html>
          <head>
            <title>Integration Test Report - {{ testRunId }}</title>
            <style>
              body { font-family: Arial, sans-serif; margin: 20px; }
              .status-passed { color: green; font-weight: bold; }
              .status-failed { color: red; font-weight: bold; }
              table { border-collapse: collapse; width: 100%; margin: 10px 0; }
              th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
              th { background-color: #f2f2f2; }
              pre { background: #f5f5f5; padding: 10px; overflow-x: auto; }
            </style>
          </head>
          <body>
            <h1>Integration Test Report</h1>
            <p><strong>Test Run ID:</strong> {{ testRunId }}</p>
            <p><strong>Service:</strong> {{ serviceName }}</p>
            <p><strong>Status:</strong> <span class="status-{{ runStatus }}">{{ runStatus }}</span></p>
            <p><strong>Started At:</strong> {{ startedAt }}</p>
            <p><strong>Verified At:</strong> {{ verifiedAt }}</p>
            <p><strong>Generated At:</strong> {{ generatedAt }}</p>

            <h2>Summary</h2>
            <ul>
              <li><strong>Total Expected:</strong> {{ totalExpected }}</li>
              <li><strong>Matched:</strong> {{ matchedCount }}</li>
              <li><strong>Missing:</strong> {{ missingCount }}</li>
              <li><strong>Unexpected:</strong> {{ unexpectedCount }}</li>
            </ul>

            <h2>Matched Events</h2>
            {{ matchedEventsTable }}

            <h2>Missing Events</h2>
            {{ missingEventsTable }}

            <h2>Unexpected Events</h2>
            {{ unexpectedEventsTable }}

            <h2>Verify Result (Raw)</h2>
            <pre>{{ verifyResultJson }}</pre>
          </body>
        </html>
        """


def _render_template(template: str, context: Dict[str, Any]) -> str:
    """
    Very naive templating: replace {{ key }} with stringified value.
    For anything more complex, you can bring in Jinja2 via your deps layer.
    """
    html = template
    for key, value in context.items():
        placeholder = "{{ " + key + " }}"
        # Convert dicts/lists to formatted JSON strings
        if isinstance(value, (dict, list)):
            value = json.dumps(value, indent=2)
        html = html.replace(placeholder, str(value))
    return html


def _get_run_meta(test_run_id: str) -> Dict[str, Any]:
    """Get run meta from DynamoDB."""
    resp = table.get_item(Key={"testId": f"run#{test_run_id}", "sk": "run#meta"})
    return resp.get("Item", {})


def _get_matched_events(test_run_id: str) -> List[Dict[str, Any]]:
    """Get all matched events (status=matched) for this run."""
    try:
        resp = table.query(
            KeyConditionExpression=Key("testId").eq(f"run#{test_run_id}")
            & Key("sk").begins_with("event#"),
            FilterExpression=Attr("status").eq("matched"),
        )
        items = resp.get("Items", [])
        # Sort by expectedOrder
        items.sort(key=lambda x: x.get("expectedOrder", 999))
        return items
    except Exception as e:
        logger.error(f"Failed to query matched events: {e}")
        return []


def _get_missing_events(test_run_id: str) -> List[Dict[str, Any]]:
    """Get all missing events (expectation#*-missing) for this run."""
    try:
        resp = table.query(
            KeyConditionExpression=Key("testId").eq(f"run#{test_run_id}")
            & Key("sk").begins_with("expectation#"),
            FilterExpression=Attr("status").eq("missed"),
        )
        items = resp.get("Items", [])
        # Sort by expectedOrder
        items.sort(key=lambda x: x.get("expectedOrder", 999))
        return items
    except Exception as e:
        logger.error(f"Failed to query missing events: {e}")
        return []


def _get_unexpected_events(test_run_id: str) -> List[Dict[str, Any]]:
    """Get all unexpected events (status=unexpected) for this run."""
    try:
        resp = table.query(
            KeyConditionExpression=Key("testId").eq(f"run#{test_run_id}")
            & Key("sk").begins_with("event#"),
            FilterExpression=Attr("status").eq("unexpected"),
        )
        items = resp.get("Items", [])
        # Sort by receivedAt
        items.sort(key=lambda x: x.get("receivedAt", ""))
        return items
    except Exception as e:
        logger.error(f"Failed to query unexpected events: {e}")
        return []


def _format_events_table(events: List[Dict[str, Any]], event_type: str) -> str:
    """Format events as HTML table."""
    if not events:
        return f"<p>No {event_type} events.</p>"

    html = "<table><tr>"
    if event_type == "matched":
        html += "<th>Order</th><th>Detail Type</th><th>Source</th><th>Event ID</th><th>Received At</th><th>Verifier At</th>"
    elif event_type == "missing":
        html += "<th>Order</th><th>Detail Type</th><th>Source</th><th>Expected Event</th><th>Verifier At</th>"
    else:  # unexpected
        html += "<th>Detail Type</th><th>Source</th><th>Event ID</th><th>Received At</th>"
    html += "</tr>"

    for event in events:
        html += "<tr>"
        if event_type == "matched":
            html += f"<td>{event.get('expectedOrder', 'N/A')}</td>"
            html += f"<td>{event.get('detailType', 'N/A')}</td>"
            html += f"<td>{event.get('source', 'N/A')}</td>"
            html += f"<td>{event.get('eventId', 'N/A')}</td>"
            html += f"<td>{event.get('receivedAt', 'N/A')}</td>"
            html += f"<td>{event.get('verifierAt', 'N/A')}</td>"
        elif event_type == "missing":
            html += f"<td>{event.get('expectedOrder', 'N/A')}</td>"
            html += f"<td>{event.get('detailType', 'N/A')}</td>"
            html += f"<td>{event.get('source', 'N/A')}</td>"
            expected = event.get("expectedEvent", {})
            html += f"<td><pre>{json.dumps(expected, indent=2)}</pre></td>"
            html += f"<td>{event.get('verifierAt', 'N/A')}</td>"
        else:  # unexpected
            html += f"<td>{event.get('detailType', 'N/A')}</td>"
            html += f"<td>{event.get('source', 'N/A')}</td>"
            html += f"<td>{event.get('eventId', 'N/A')}</td>"
            html += f"<td>{event.get('receivedAt', 'N/A')}</td>"
        html += "</tr>"

    html += "</table>"
    return html


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Input (from Step Functions ReportRun task):

      {
        "testRunId": "...",  # or "runId"
        "serviceName": "workflowrunmanager",
        "verifyResult": { ... }
      }
    """
    test_run_id = event.get("testRunId") or event.get("runId")
    if not test_run_id:
        raise ValueError("testRunId or runId is required")

    verify_result = event.get("verifyResult", {})

    # Load run meta to get additional details
    run_meta = _get_run_meta(test_run_id)
    service_name = run_meta.get("serviceName") or event.get("serviceName", "all")
    started_at = run_meta.get("startedAt", "")
    verified_at = run_meta.get("verifiedAt", "")

    # Get detailed event information from DynamoDB
    matched_events = _get_matched_events(test_run_id)
    missing_events = _get_missing_events(test_run_id)
    unexpected_events = _get_unexpected_events(test_run_id)

    now = datetime.now(timezone.utc)
    ts_for_filename = _safe_timestamp_filename(now)
    yyyy = now.strftime("%Y")
    mm = now.strftime("%m")
    dd = now.strftime("%d")

    # reports/testruns/{serviceName}/{YYYY}/{MM}/{DD}/{timestamp}-{testRunId}.html
    key = (
        f"reports/testruns/{service_name}/"
        f"{yyyy}/{mm}/{dd}/"
        f"{ts_for_filename}-{test_run_id}.html"
    )

    logger.info(
        "Generating report for testRunId=%s, serviceName=%s -> s3://%s/%s",
        test_run_id,
        service_name,
        S3_BUCKET,
        key,
    )

    template = _load_template()

    # Format event tables
    matched_table = _format_events_table(matched_events, "matched")
    missing_table = _format_events_table(missing_events, "missing")
    unexpected_table = _format_events_table(unexpected_events, "unexpected")

    run_status = verify_result.get("runStatus", "unknown")
    matched_count = verify_result.get("matchedCount", 0)
    missing_count = verify_result.get("missingCount", 0)
    unexpected_count = verify_result.get("unexpectedCount", 0)
    total_expected = verify_result.get("totalExpected", 0)

    context = {
        "testRunId": test_run_id,
        "serviceName": service_name,
        "runStatus": run_status,
        "startedAt": started_at,
        "verifiedAt": verified_at,
        "generatedAt": now.isoformat(),
        "totalExpected": total_expected,
        "matchedCount": matched_count,
        "missingCount": missing_count,
        "unexpectedCount": unexpected_count,
        "matchedEventsTable": matched_table,
        "missingEventsTable": missing_table,
        "unexpectedEventsTable": unexpected_table,
        "verifyResultJson": json.dumps(verify_result, indent=2),
    }

    html = _render_template(template, context)

    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=html.encode("utf-8"),
        ContentType="text/html",
    )

    # Update run meta with reportS3Key
    try:
        table.update_item(
            Key={"testId": f"run#{test_run_id}", "sk": "run#meta"},
            UpdateExpression="SET reportS3Key = :key",
            ExpressionAttributeValues={":key": key},
        )
        logger.info(f"Updated run meta with reportS3Key: {key}")
    except Exception as e:
        logger.error(f"Failed to update run meta with reportS3Key: {e}")

    return {
        "bucket": S3_BUCKET,
        "key": key,
        "url": f"s3://{S3_BUCKET}/{key}",
    }
