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
from utils.reporter_template import load_reporter_template
from services.aws.dynamodb import (
    get_run_meta,
    get_items_from_dynamodb,
    update_item_to_dynamodb,
)
from services.aws.s3 import S3_BUCKET, store_item_to_s3
from utils.utils import get_safe_timestamp_filename, parse_iso_safe

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _get_verify_result_value(
    verify_result: Dict[str, Any], *keys: str, default: Any = 0
) -> Any:
    """Get value from verify_result, trying multiple key names (e.g. matchedCount vs matchedEventsCount)."""
    for key in keys:
        val = verify_result.get(key)
        if val is not None:
            return val
    return default


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


def _get_matched_events(test_id: str) -> List[Dict[str, Any]]:
    """
    Get all matched events (status=matched) for this run.

    Args:
        test_id: The test ID (format: "run#YYMMDD_A00001_XXXX_TESTXXXXXX")
    """
    try:
        items = get_items_from_dynamodb(
            KeyConditionExpression=Key("testId").eq(f"run#{test_id}")
            & Key("sk").begins_with("event#"),
            FilterExpression=Attr("status").eq("matched"),
        )
        # Sort by expectedOrder
        items.sort(key=lambda x: x.get("expectedOrder", 999))
        return items
    except Exception as e:
        logger.error(f"Failed to query matched events: {e}")
        return []


def _get_missing_events(test_id: str) -> List[Dict[str, Any]]:
    """
    Get all missing events (event#*-missing) for this run.

    Args:
        test_instrument_run_id: The test instrument run ID (format: "YYMMDD_A00001_XXXX_TESTXXXXXX")
    """
    try:
        items = get_items_from_dynamodb(
            KeyConditionExpression=Key("testId").eq(f"run#{test_id}")
            & Key("sk").begins_with("event#"),
            FilterExpression=Attr("status").eq("missed"),
        )
        # Sort by expectedOrder
        items.sort(key=lambda x: x.get("expectedOrder", 999))
        return items
    except Exception as e:
        logger.error(f"Failed to query missing events: {e}")
        return []


def _get_unexpected_events(test_id: str) -> List[Dict[str, Any]]:
    """
    Get all unexpected events (status=unexpected) for this run.

    Args:
        test_instrument_run_id: The test instrument run ID (format: "YYMMDD_A00001_XXXX_TESTXXXXXX")
    """
    try:
        items = get_items_from_dynamodb(
            KeyConditionExpression=Key("testId").eq(f"run#{test_id}")
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


def generate_report_for_all_services(verify_results: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generate a report for all services.
    """

    test_ids = set()
    service_name_for_report = "all"
    run_status = "passed"  # Start optimistic; set to failed if any service fails
    started_at_dt = None
    verified_at_dt = None
    total_expected = 0
    matched_count = 0
    missing_count = 0
    unexpected_count = 0
    matched_events = []
    missing_events = []
    unexpected_events = []
    matched_table = ""
    missing_table = ""
    unexpected_table = ""
    verify_result_json = ""

    # accumulate the report context
    for service_name, verify_result in verify_results.items():
        if isinstance(verify_result, dict) and "error" in verify_result:
            logger.warning(
                f"Skipping service {service_name} due to error: {verify_result.get('error')}"
            )
            run_status = "failed"
            continue
        logger.info(
            f"Generating report for serviceName={service_name}, verifyResult={json.dumps(verify_result, indent=2)}"
        )
        test_id = verify_result.get("runId", "")
        if not test_id:
            raise ValueError(f"testId (runId) is required for service {service_name}")
        test_ids.add(test_id)

        # Run status: any failure means overall failure
        if verify_result.get("runStatus", "unknown") != "passed":
            run_status = "failed"

        # Get startedAt/verifiedAt from run_meta (verify_result may not have them)
        run_meta = get_run_meta(test_id)
        if run_meta:
            sr_at = parse_iso_safe(run_meta.get("startedAt", ""))
            vr_at = parse_iso_safe(run_meta.get("verifiedAt", ""))
            if sr_at:
                started_at_dt = (
                    sr_at if started_at_dt is None else min(started_at_dt, sr_at)
                )
            if vr_at:
                verified_at_dt = (
                    vr_at if verified_at_dt is None else max(verified_at_dt, vr_at)
                )

        # Support both key naming conventions (matchedCount vs matchedEventsCount, etc.)
        total_expected += _get_verify_result_value(
            verify_result, "totalExpected", "totalExpectedEventsCount"
        )
        matched_count += _get_verify_result_value(
            verify_result, "matchedCount", "matchedEventsCount"
        )
        missing_count += _get_verify_result_value(
            verify_result, "missingCount", "missingEventsCount"
        )
        unexpected_count += _get_verify_result_value(
            verify_result, "unexpectedCount", "unexpectedEventsCount"
        )

        # accumulate the events
        matched_events.extend(_get_matched_events(test_id))
        missing_events.extend(_get_missing_events(test_id))
        unexpected_events.extend(_get_unexpected_events(test_id))

    # format the events tables
    matched_table = _format_events_table(matched_events, "matched", S3_BUCKET)
    missing_table = _format_events_table(missing_events, "missing", S3_BUCKET)
    unexpected_table = _format_events_table(unexpected_events, "unexpected", S3_BUCKET)

    started_at_str = (
        started_at_dt.isoformat().replace("+00:00", "Z") if started_at_dt else ""
    )
    verified_at_str = (
        verified_at_dt.isoformat().replace("+00:00", "Z") if verified_at_dt else ""
    )

    verify_result_json = json.dumps(verify_results, indent=2)
    context = {
        "testId": ", ".join(sorted(test_ids)),
        "serviceName": service_name_for_report,
        "runStatus": run_status,
        "startedAt": started_at_str,
        "verifiedAt": verified_at_str,
        "totalExpected": total_expected,
        "matchedCount": matched_count,
        "missingCount": missing_count,
        "unexpectedCount": unexpected_count,
        "matchedEventsTable": matched_table,
        "missingEventsTable": missing_table,
        "unexpectedEventsTable": unexpected_table,
        "verifyResultJson": verify_result_json,
    }

    now = datetime.now(timezone.utc)
    ts_for_filename = get_safe_timestamp_filename(now)
    yyyy = now.strftime("%Y")
    mm = now.strftime("%m")
    dd = now.strftime("%d")

    # reports/testruns/year={YYYY}/month={MM}/day={DD}/serviceName/{testId}-{timestamp}.html
    key = (
        f"reports/testruns/"
        f"year={yyyy}/month={mm}/day={dd}/"
        f"{service_name_for_report}/"
        f"all-{ts_for_filename}.html"
    )

    logger.info(
        "Generating report for serviceName=%s -> s3://%s/%s",
        service_name_for_report,
        S3_BUCKET,
        key,
    )

    template = load_reporter_template()
    html = _render_template(template, context)

    store_item_to_s3(
        key=key,
        body=html,
    )

    for service_name, verify_result in verify_results.items():
        test_id = verify_result.get("runId", "")
        if not test_id:
            raise ValueError("testId is required")
        update_item_to_dynamodb(
            key={"testId": f"run#{test_id}", "sk": "run#meta"},
            UpdateExpression="SET #reportS3Key = :key",
            ExpressionAttributeNames={"#reportS3Key": "reportS3Key"},
            ExpressionAttributeValues={":key": key},
        )

    return {
        "bucket": S3_BUCKET,
        "key": key,
        "reportUrl": f"s3://{S3_BUCKET}/{key}",
    }


def generate_report_for_service(
    service_name: str, verify_result: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Generate a report for a specific service.
    """
    test_id = verify_result.get("runId", "")
    if not test_id:
        raise ValueError("testId (runId) is required")
    # Load run meta to get additional details
    run_meta = get_run_meta(test_id) or {}
    started_at = run_meta.get("startedAt", "")
    verified_at = run_meta.get("verifiedAt", "")

    now = datetime.now(timezone.utc)
    ts_for_filename = get_safe_timestamp_filename(now)
    yyyy = now.strftime("%Y")
    mm = now.strftime("%m")
    dd = now.strftime("%d")

    # reports/testruns/year={YYYY}/month={MM}/day={DD}/serviceName/{testId}-{timestamp}.html
    key = (
        f"reports/testruns/"
        f"year={yyyy}/month={mm}/day={dd}/"
        f"{service_name}/"
        f"{test_id}-{ts_for_filename}.html"
    )

    logger.info(
        "Generating report for testId=%s, serviceName=%s -> s3://%s/%s",
        test_id,
        service_name,
        S3_BUCKET,
        key,
    )

    template = load_reporter_template()

    matched_events = _get_matched_events(test_id)
    missing_events = _get_missing_events(test_id)
    unexpected_events = _get_unexpected_events(test_id)

    # Format event tables with S3 bucket for links
    matched_table = _format_events_table(matched_events, "matched", S3_BUCKET)
    missing_table = _format_events_table(missing_events, "missing", S3_BUCKET)
    unexpected_table = _format_events_table(unexpected_events, "unexpected", S3_BUCKET)

    run_status = verify_result.get("runStatus", "unknown")
    matched_count = _get_verify_result_value(
        verify_result, "matchedCount", "matchedEventsCount"
    )
    missing_count = _get_verify_result_value(
        verify_result, "missingCount", "missingEventsCount"
    )
    unexpected_count = _get_verify_result_value(
        verify_result, "unexpectedCount", "unexpectedEventsCount"
    )
    total_expected = _get_verify_result_value(
        verify_result, "totalExpected", "totalExpectedEventsCount"
    )

    context = {
        "testId": test_id,
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
        update_item_to_dynamodb(
            key={"testId": f"run#{test_id}", "sk": "run#meta"},
            UpdateExpression="SET #reportS3Key = :key",
            ExpressionAttributeNames={"#reportS3Key": "reportS3Key"},
            ExpressionAttributeValues={":key": key},
        )
        logger.info(f"Updated run meta with reportS3Key: {key}")
    except Exception as e:
        logger.error(f"Failed to update run meta with reportS3Key: {e}")

    return {
        "bucket": S3_BUCKET,
        "key": key,
        "reportUrl": f"s3://{S3_BUCKET}/{key}",
    }


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Input (from Step Functions ReportRun task):
    when service name is all, means the report is for all services
    {
      "serviceName": "all",
      "verifyResult": {
        "sequencerunmanager": { ... },
        "workflowrunmanager": { ... },
        ...
      }
    }

    when service name is not all, means the report is for a specific service
    verifyResult is keyed by service name: { "workflowrunmanager": { runId, runStatus, ... } }
    """
    service_name = event.get("serviceName", "all")
    verify_result = event.get("verifyResult", {})

    # Step Functions JsonPath.stringAt may pass verifyResult as JSON string
    if isinstance(verify_result, str):
        try:
            verify_result = json.loads(verify_result)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse verifyResult JSON: {e}")
            raise ValueError("verifyResult must be valid JSON")

    if service_name == "all":
        return generate_report_for_all_services(verify_result)
    else:
        # Verifier returns { service_name: result }; fallback to verify_result if flat
        vr = verify_result.get(service_name, verify_result)
        if not isinstance(vr, dict):
            raise ValueError(f"Invalid verifyResult for service {service_name}")
        return generate_report_for_service(service_name, vr)
