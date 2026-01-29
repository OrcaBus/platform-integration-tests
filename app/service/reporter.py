# app/service/reporter.py
"""
Reporter Lambda:

- Input (from Step Functions):
  {
    "testInstrumentRunId": "...",  # or "testRunId" or "runId" for backward compatibility
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

Note: testInstrumentRunId format is "YYMMDD_A00001_XXXX_TESTXXXXXX" (e.g., "260122_A00001_1234_TEST123456")
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import quote

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
        # Enhanced fallback template with modern design
        return """
        <!DOCTYPE html>
        <html lang="en">
          <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Integration Test Report - {{ testInstrumentRunId }}</title>
            <style>
              * { box-sizing: border-box; margin: 0; padding: 0; }
              body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
                line-height: 1.6;
                color: #333;
              }
              .container {
                max-width: 1400px;
                margin: 0 auto;
                background: white;
                border-radius: 12px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                overflow: hidden;
              }
              .header {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 40px;
                text-align: center;
              }
              .header h1 {
                font-size: 2.5em;
                margin-bottom: 10px;
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 15px;
              }
              .header .icon {
                font-size: 1.2em;
              }
              .header-info {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 20px;
                margin-top: 30px;
                padding-top: 30px;
                border-top: 1px solid rgba(255,255,255,0.2);
              }
              .header-info-item {
                text-align: left;
              }
              .header-info-item strong {
                display: block;
                opacity: 0.9;
                font-size: 0.9em;
                margin-bottom: 5px;
              }
              .header-info-item span {
                font-size: 1.1em;
                word-break: break-word;
                overflow-wrap: break-word;
              }
              .content {
                padding: 40px;
              }
              .status-badge {
                display: inline-block;
                padding: 8px 20px;
                border-radius: 20px;
                font-weight: bold;
                font-size: 0.9em;
                text-transform: uppercase;
                letter-spacing: 1px;
              }
              .status-passed {
                background: #10b981;
                color: white;
              }
              .status-failed {
                background: #ef4444;
                color: white;
              }
              .status-timeout {
                background: #f59e0b;
                color: white;
              }
              .status-running {
                background: #3b82f6;
                color: white;
              }
              .summary-section {
                background: #f8fafc;
                border-radius: 8px;
                padding: 30px;
                margin: 30px 0;
                border-left: 4px solid #667eea;
              }
              .summary-section h2 {
                font-size: 1.8em;
                margin-bottom: 20px;
                color: #1e293b;
                display: flex;
                align-items: center;
                gap: 10px;
              }
              .summary-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 20px;
                margin-top: 20px;
              }
              .summary-card {
                background: white;
                padding: 20px;
                border-radius: 8px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                text-align: center;
                transition: transform 0.2s;
              }
              .summary-card:hover {
                transform: translateY(-2px);
                box-shadow: 0 4px 12px rgba(0,0,0,0.15);
              }
              .summary-card .value {
                font-size: 2.5em;
                font-weight: bold;
                color: #667eea;
                margin: 10px 0;
              }
              .summary-card .label {
                color: #64748b;
                font-size: 0.9em;
                text-transform: uppercase;
                letter-spacing: 1px;
              }
              .summary-card.matched .value { color: #10b981; }
              .summary-card.missing .value { color: #ef4444; }
              .summary-card.unexpected .value { color: #f59e0b; }
              .events-section {
                margin: 40px 0;
                padding: 30px;
                background: #f8fafc;
                border-radius: 8px;
              }
              .events-section h2 {
                font-size: 1.8em;
                margin-bottom: 25px;
                color: #1e293b;
                display: flex;
                align-items: center;
                gap: 10px;
                padding-bottom: 15px;
                border-bottom: 2px solid #e2e8f0;
              }
              .events-table {
                width: 100%;
                border-collapse: collapse;
                background: white;
                border-radius: 8px;
                overflow: hidden;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                margin-top: 20px;
              }
              .events-table thead {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
              }
              .events-table th {
                padding: 15px;
                text-align: left;
                font-weight: 600;
                text-transform: uppercase;
                font-size: 0.85em;
                letter-spacing: 0.5px;
              }
              .events-table tbody tr {
                border-bottom: 1px solid #e2e8f0;
                transition: background 0.2s;
              }
              .events-table tbody tr:hover {
                background: #f8fafc;
              }
              .events-table tbody tr:last-child {
                border-bottom: none;
              }
              .events-table td {
                padding: 15px;
                vertical-align: top;
              }
              .events-table code {
                background: #f1f5f9;
                padding: 4px 8px;
                border-radius: 4px;
                font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
                font-size: 0.9em;
                color: #475569;
              }
              .events-table code.event-id {
                background: #dbeafe;
                color: #1e40af;
                word-break: break-all;
              }
              .events-table .order-col {
                text-align: center;
                font-weight: bold;
                color: #667eea;
                width: 60px;
              }
              .events-table .timestamp {
                font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
                font-size: 0.85em;
                color: #64748b;
              }
              .events-table .expected-event {
                max-width: 500px;
              }
              .events-table pre {
                background: #1e293b;
                color: #e2e8f0;
                padding: 15px;
                border-radius: 6px;
                overflow-x: auto;
                font-size: 0.85em;
                line-height: 1.5;
                margin: 0;
              }
              .s3-link {
                text-align: center;
                width: 50px;
              }
              .s3-link a {
                display: inline-block;
                text-decoration: none;
                font-size: 1.2em;
                color: #667eea;
                transition: transform 0.2s, color 0.2s;
              }
              .s3-link a:hover {
                transform: scale(1.2);
                color: #764ba2;
              }
              .s3-link a:visited {
                color: #667eea;
              }
              .empty-state {
                text-align: center;
                padding: 40px;
                color: #64748b;
              }
              .empty-state .icon {
                font-size: 3em;
                display: block;
                margin-bottom: 10px;
              }
              .raw-result {
                background: #1e293b;
                color: #e2e8f0;
                padding: 25px;
                border-radius: 8px;
                margin-top: 30px;
              }
              .raw-result h2 {
                color: #e2e8f0;
                margin-bottom: 15px;
                font-size: 1.5em;
              }
              .raw-result pre {
                background: #0f172a;
                padding: 20px;
                border-radius: 6px;
                overflow-x: auto;
                font-size: 0.9em;
                line-height: 1.6;
              }
              @media (max-width: 768px) {
                .header h1 { font-size: 1.8em; }
                .content { padding: 20px; }
                .summary-grid { grid-template-columns: 1fr; }
                .events-table { font-size: 0.85em; }
                .events-table th,
                .events-table td { padding: 10px 8px; }
              }
            </style>
          </head>
          <body>
            <div class="container">
              <div class="header">
                <h1>
                  <span class="icon">📊</span>
                  Integration Test Report
                </h1>
                <div class="header-info">
                  <div class="header-info-item">
                    <strong>Test Instrument Run ID</strong>
                    <span>{{ testInstrumentRunId }}</span>
                  </div>
                  <div class="header-info-item">
                    <strong>Service</strong>
                    <span>{{ serviceName }}</span>
                  </div>
                  <div class="header-info-item">
                    <strong>Status</strong>
                    <span class="status-badge status-{{ runStatus }}">{{ runStatus }}</span>
                  </div>
                  <div class="header-info-item">
                    <strong>Started At</strong>
                    <span>{{ startedAt }}</span>
                  </div>
                  <div class="header-info-item">
                    <strong>Verified At</strong>
                    <span>{{ verifiedAt }}</span>
                  </div>
                </div>
              </div>

              <div class="content">
                <div class="summary-section">
                  <h2><span>📈</span> Summary</h2>
                  <div class="summary-grid">
                    <div class="summary-card">
                      <div class="label">Total Expected</div>
                      <div class="value">{{ totalExpected }}</div>
                    </div>
                    <div class="summary-card matched">
                      <div class="label">✓ Matched</div>
                      <div class="value">{{ matchedCount }}</div>
                    </div>
                    <div class="summary-card missing">
                      <div class="label">✗ Missing</div>
                      <div class="value">{{ missingCount }}</div>
                    </div>
                    <div class="summary-card unexpected">
                      <div class="label">⚠ Unexpected</div>
                      <div class="value">{{ unexpectedCount }}</div>
                    </div>
                  </div>
                </div>

                <div class="events-section">
                  <h2><span>✅</span> Matched Events</h2>
                  {{ matchedEventsTable }}
                </div>

                <div class="events-section">
                  <h2><span>❌</span> Missing Events</h2>
                  {{ missingEventsTable }}
                </div>

                <div class="events-section">
                  <h2><span>⚠️</span> Unexpected Events</h2>
                  {{ unexpectedEventsTable }}
                </div>

                <div class="raw-result">
                  <h2>🔍 Verify Result (Raw JSON)</h2>
                  <pre>{{ verifyResultJson }}</pre>
                </div>
              </div>
            </div>
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


def _get_run_meta(test_instrument_run_id: str) -> Dict[str, Any]:
    """
    Get run meta from DynamoDB.

    Args:
        test_instrument_run_id: The test instrument run ID (format: "YYMMDD_A00001_XXXX_TESTXXXXXX")
    """
    resp = table.get_item(
        Key={"testId": f"run#{test_instrument_run_id}", "sk": "run#meta"}
    )
    return resp.get("Item", {})


def _get_matched_events(test_instrument_run_id: str) -> List[Dict[str, Any]]:
    """
    Get all matched events (status=matched) for this run.

    Args:
        test_instrument_run_id: The test instrument run ID (format: "YYMMDD_A00001_XXXX_TESTXXXXXX")
    """
    try:
        resp = table.query(
            KeyConditionExpression=Key("testId").eq(f"run#{test_instrument_run_id}")
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


def _get_missing_events(test_instrument_run_id: str) -> List[Dict[str, Any]]:
    """
    Get all missing events (expectation#*-missing) for this run.

    Args:
        test_instrument_run_id: The test instrument run ID (format: "YYMMDD_A00001_XXXX_TESTXXXXXX")
    """
    try:
        resp = table.query(
            KeyConditionExpression=Key("testId").eq(f"run#{test_instrument_run_id}")
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


def _get_unexpected_events(test_instrument_run_id: str) -> List[Dict[str, Any]]:
    """
    Get all unexpected events (status=unexpected) for this run.

    Args:
        test_instrument_run_id: The test instrument run ID (format: "YYMMDD_A00001_XXXX_TESTXXXXXX")
    """
    try:
        resp = table.query(
            KeyConditionExpression=Key("testId").eq(f"run#{test_instrument_run_id}")
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


def _format_events_table(
    events: List[Dict[str, Any]], event_type: str, s3_bucket: str = None
) -> str:
    """Format events as HTML table with improved styling and S3 links."""
    if not events:
        icon = (
            "✓" if event_type == "matched" else "✗" if event_type == "missing" else "⚠"
        )
        return f'<div class="empty-state"><span class="icon">{icon}</span><p>No {event_type} events.</p></div>'

    html = '<table class="events-table">'
    html += "<thead><tr>"
    if event_type == "matched":
        html += "<th>#</th><th>Detail Type</th><th>Source</th><th>Event ID</th><th>Received At</th><th>Verified At</th><th>S3</th>"
    elif event_type == "missing":
        html += "<th>#</th><th>Detail Type</th><th>Source</th><th>Expected Event</th><th>Verified At</th><th>S3</th>"
    else:  # unexpected
        html += "<th>Detail Type</th><th>Source</th><th>Event ID</th><th>Received At</th><th>S3</th>"
    html += "</tr></thead><tbody>"

    for idx, event in enumerate(events, 1):
        html += "<tr>"
        # Get S3 key for the event
        s3_key = event.get("rawS3Key", "")
        s3_link_html = ""
        if s3_key and s3_bucket:
            # URL encode the S3 key for the console URL
            encoded_key = quote(s3_key, safe="/")
            s3_url = f"https://s3.console.aws.amazon.com/s3/object/{s3_bucket}?prefix={encoded_key}"
            s3_link_html = f'<td class="s3-link"><a href="{s3_url}" target="_blank" title="View in S3: s3://{s3_bucket}/{s3_key}">🔗</a></td>'
        else:
            s3_link_html = '<td class="s3-link">-</td>'

        if event_type == "matched":
            html += f'<td class="order-col">{event.get("expectedOrder", idx)}</td>'
            html += f'<td><code>{event.get("detailType", "N/A")}</code></td>'
            html += f'<td><code>{event.get("source", "N/A")}</code></td>'
            html += (
                f'<td><code class="event-id">{event.get("eventId", "N/A")}</code></td>'
            )
            html += f'<td class="timestamp">{event.get("receivedAt", "N/A")}</td>'
            html += f'<td class="timestamp">{event.get("verifiedAt", "N/A")}</td>'
            html += s3_link_html
        elif event_type == "missing":
            html += f'<td class="order-col">{event.get("expectedOrder", idx)}</td>'
            html += f'<td><code>{event.get("detailType", "N/A")}</code></td>'
            html += f'<td><code>{event.get("source", "N/A")}</code></td>'
            expected = event.get("expectedEvent", {})
            html += f'<td class="expected-event"><pre>{json.dumps(expected, indent=2)}</pre></td>'
            html += f'<td class="timestamp">{event.get("verifiedAt", "N/A")}</td>'
            html += s3_link_html
        else:  # unexpected
            html += f'<td><code>{event.get("detailType", "N/A")}</code></td>'
            html += f'<td><code>{event.get("source", "N/A")}</code></td>'
            html += (
                f'<td><code class="event-id">{event.get("eventId", "N/A")}</code></td>'
            )
            html += f'<td class="timestamp">{event.get("receivedAt", "N/A")}</td>'
            html += s3_link_html
        html += "</tr>"

    html += "</tbody></table>"
    return html


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Input (from Step Functions ReportRun task):

      {
        "testInstrumentRunId": "...",  # or "testRunId" or "runId" for backward compatibility
        "serviceName": "workflowrunmanager",
        "verifyResult": { ... }
      }

    Note: The Step Functions now passes testInstrumentRunId from seedResult.
    """
    # Extract test instrument run ID from various possible field names
    # Priority: testInstrumentRunId > testRunId > runId
    test_instrument_run_id = (
        event.get("testInstrumentRunId") or event.get("testRunId") or event.get("runId")
    )

    if not test_instrument_run_id:
        raise ValueError("testInstrumentRunId, testRunId, or runId is required")

    verify_result = event.get("verifyResult", {})

    # Load run meta to get additional details
    run_meta = _get_run_meta(test_instrument_run_id)
    service_name = run_meta.get("serviceName") or event.get("serviceName", "all")
    started_at = run_meta.get("startedAt", "")
    verified_at = run_meta.get("verifiedAt", "")

    # Get detailed event information from DynamoDB
    matched_events = _get_matched_events(test_instrument_run_id)
    missing_events = _get_missing_events(test_instrument_run_id)
    unexpected_events = _get_unexpected_events(test_instrument_run_id)

    now = datetime.now(timezone.utc)
    ts_for_filename = _safe_timestamp_filename(now)
    yyyy = now.strftime("%Y")
    mm = now.strftime("%m")
    dd = now.strftime("%d")

    # reports/testruns/{serviceName}/year={YYYY}/month={MM}/day={DD}/{timestamp}-{testInstrumentRunId}.html
    key = (
        f"reports/{service_name}/"
        f"year={yyyy}/month={mm}/day={dd}/"
        f"{ts_for_filename}-{test_instrument_run_id}.html"
    )

    logger.info(
        "Generating report for testInstrumentRunId=%s, serviceName=%s -> s3://%s/%s",
        test_instrument_run_id,
        service_name,
        S3_BUCKET,
        key,
    )

    template = _load_template()

    # Format event tables with S3 bucket for links
    matched_table = _format_events_table(matched_events, "matched", S3_BUCKET)
    missing_table = _format_events_table(missing_events, "missing", S3_BUCKET)
    unexpected_table = _format_events_table(unexpected_events, "unexpected", S3_BUCKET)

    run_status = verify_result.get("runStatus", "unknown")
    matched_count = verify_result.get("matchedCount", 0)
    missing_count = verify_result.get("missingCount", 0)
    unexpected_count = verify_result.get("unexpectedCount", 0)
    total_expected = verify_result.get("totalExpected", 0)

    context = {
        "testInstrumentRunId": test_instrument_run_id,
        "testRunId": test_instrument_run_id,  # Keep for backward compatibility with template
        "serviceName": service_name,
        "runStatus": run_status,
        "startedAt": started_at,
        "verifiedAt": verified_at,
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
            Key={"testId": f"run#{test_instrument_run_id}", "sk": "run#meta"},
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
