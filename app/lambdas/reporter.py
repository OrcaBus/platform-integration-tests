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
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import quote

from boto3.dynamodb.conditions import Key, Attr
from services.reporter_template import load_reporter_template
from services.dynamodb import (
    get_run_meta,
    get_items_from_dynamodb,
    update_item_in_dynamodb,
)
from services.s3 import S3_BUCKET, store_item_to_s3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _safe_timestamp_filename(dt: datetime) -> str:
    """
    Convert datetime to a filename-safe ISO-ish string:
    2025-11-21T10-15-32Z
    """
    return dt.strftime("%Y-%m-%dT%H-%M-%SZ")


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


def _get_matched_events(test_instrument_run_id: str) -> List[Dict[str, Any]]:
    """
    Get all matched events (status=matched) for this run.

    Args:
        test_instrument_run_id: The test instrument run ID (format: "YYMMDD_A00001_XXXX_TESTXXXXXX")
    """
    try:
        items = get_items_from_dynamodb(
            KeyConditionExpression=Key("testId").eq(f"run#{test_instrument_run_id}")
            & Key("sk").begins_with("event#"),
            FilterExpression=Attr("status").eq("matched"),
        )
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
        items = get_items_from_dynamodb(
            KeyConditionExpression=Key("testId").eq(f"run#{test_instrument_run_id}")
            & Key("sk").begins_with("expectation#"),
            FilterExpression=Attr("status").eq("missed"),
        )
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
        items = get_items_from_dynamodb(
            KeyConditionExpression=Key("testId").eq(f"run#{test_instrument_run_id}")
            & Key("sk").begins_with("event#"),
            FilterExpression=Attr("status").eq("unexpected"),
        )
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
    run_meta = get_run_meta(test_instrument_run_id)
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
        f"reports/testruns/{service_name}/{test_instrument_run_id}/"
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

    template = load_reporter_template()

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

    store_item_to_s3(
        key=key,
        body=html,
    )

    # Update run meta with reportS3Key
    try:
        update_item_in_dynamodb(
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
