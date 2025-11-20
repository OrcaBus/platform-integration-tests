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
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

import boto3
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
          <head><title>Integration Test Report - {{ testRunId }}</title></head>
          <body>
            <h1>Integration Test Report</h1>
            <p><strong>Test Run ID:</strong> {{ testRunId }}</p>
            <p><strong>Service:</strong> {{ serviceName }}</p>
            <p><strong>Started At:</strong> {{ startedAt }}</p>
            <p><strong>Generated At:</strong> {{ generatedAt }}</p>
            <h2>Verify Result</h2>
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
        html = html.replace(placeholder, str(value))
    return html


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Input (from Step Functions ReportRun task):

      {
        "testRunId": "...",
        "serviceName": "workflowrunmanager",
        "verifyResult": { ... }
      }
    """
    test_run_id = event["testRunId"]
    service_name = event.get("serviceName", "all")
    verify_result = event.get("verifyResult", {})

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

    context = {
        "testRunId": test_run_id,
        "serviceName": service_name,
        "startedAt": verify_result.get("startedAt", ""),  # optional field
        "generatedAt": now.isoformat(),
        "verifyResultJson": json.dumps(verify_result, indent=2),
    }

    html = _render_template(template, context)

    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=html.encode("utf-8"),
        ContentType="text/html",
    )

    # If you want SFN to know where the report is:
    return {
        "bucket": S3_BUCKET,
        "key": key,
        "url": f"s3://{S3_BUCKET}/{key}",
    }
