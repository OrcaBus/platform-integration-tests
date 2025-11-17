"""
Reporter Lambda Function

Reads the verdict from DynamoDB, produces an HTML/JSON report, optionally
uploads to S3, and notifies Slack. Can also signal CodePipeline approval.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
import requests

# Initialize clients
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

# Environment variables
TABLE_NAME = os.environ.get('TABLE_NAME', 'platform-it-store')
S3_BUCKET = os.environ.get('S3_BUCKET')  # Optional
S3_PREFIX = os.environ.get('S3_PREFIX', 'reports/')
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL')  # Optional
CODEPIPELINE_NAME = os.environ.get('CODEPIPELINE_NAME')  # Optional
CODEPIPELINE_STAGE = os.environ.get('CODEPIPELINE_STAGE', 'Approval')  # Optional


def load_verdict(run_id: str) -> Optional[Dict[str, Any]]:
    """Load verdict from DynamoDB."""
    table = dynamodb.Table(TABLE_NAME)

    try:
        response = table.get_item(
            Key={
                'runId': run_id,
                'sk': 'verdict#1',
            }
        )
        return response.get('Item')
    except Exception as e:
        print(f'Error loading verdict: {str(e)}')
        return None


def load_run_metadata(run_id: str) -> Optional[Dict[str, Any]]:
    """Load run metadata from DynamoDB."""
    table = dynamodb.Table(TABLE_NAME)

    try:
        response = table.get_item(
            Key={
                'runId': run_id,
                'sk': 'run#meta',
            }
        )
        return response.get('Item')
    except Exception as e:
        print(f'Error loading run metadata: {str(e)}')
        return None


def load_fixtures(run_id: str) -> List[Dict[str, Any]]:
    """Load all expected fixtures for a run."""
    table = dynamodb.Table(TABLE_NAME)

    try:
        response = table.query(
            KeyConditionExpression='runId = :runId AND begins_with(sk, :prefix)',
            ExpressionAttributeValues={
                ':runId': run_id,
                ':prefix': 'fixture#',
            }
        )
        return response.get('Items', [])
    except Exception as e:
        print(f'Error loading fixtures: {str(e)}')
        return []


def load_observed_events(run_id: str) -> List[Dict[str, Any]]:
    """Load all observed events for a run."""
    table = dynamodb.Table(TABLE_NAME)

    try:
        response = table.query(
            KeyConditionExpression='runId = :runId AND begins_with(sk, :prefix)',
            ExpressionAttributeValues={
                ':runId': run_id,
                ':prefix': 'event#',
            }
        )
        return response.get('Items', [])
    except Exception as e:
        print(f'Error loading observed events: {str(e)}')
        return []


def generate_json_report(
    run_id: str,
    metadata: Dict[str, Any],
    verdict: Dict[str, Any],
    fixtures: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Generate JSON report."""
    return {
        'runId': run_id,
        'scenario': metadata.get('scenario'),
        'status': verdict.get('status'),
        'passed': verdict.get('passed', False),
        'startedAt': metadata.get('startedAt'),
        'completedAt': metadata.get('completedAt'),
        'verifiedAt': verdict.get('verifiedAt'),
        'metrics': verdict.get('metrics', {}),
        'checks': verdict.get('checks', {}),
        'failures': verdict.get('failures', []),
        'expectedEvents': [
            {
                'eventType': f.get('eventType'),
                'seq': f.get('seq'),
                'expectedPayload': f.get('expectedPayload', {}),
            }
            for f in fixtures
        ],
        'observedEvents': [
            {
                'eventId': e.get('eventId'),
                'eventType': e.get('eventType'),
                'seq': e.get('seq'),
                'receivedAt': e.get('receivedAt'),
                'payload': e.get('payload', {}),
            }
            for e in events
        ],
    }


def generate_html_report(
    run_id: str,
    metadata: Dict[str, Any],
    verdict: Dict[str, Any],
    fixtures: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
) -> str:
    """Generate HTML report."""
    status = verdict.get('status', 'unknown')
    passed = verdict.get('passed', False)
    status_color = 'green' if passed else 'red'
    status_emoji = '✅' if passed else '❌'

    checks = verdict.get('checks', {})
    failures = verdict.get('failures', [])
    metrics = verdict.get('metrics', {})

    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Integration Test Report - {run_id}</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #333;
            border-bottom: 2px solid #{status_color};
            padding-bottom: 10px;
        }}
        .status {{
            font-size: 24px;
            font-weight: bold;
            color: {status_color};
            margin: 20px 0;
        }}
        .section {{
            margin: 20px 0;
            padding: 15px;
            background: #f9f9f9;
            border-radius: 4px;
        }}
        .check {{
            margin: 10px 0;
            padding: 10px;
            background: white;
            border-left: 4px solid {'green' if checks.get('presence', {}).get('passed') else 'red'};
        }}
        .failure {{
            background: #ffe6e6;
            padding: 10px;
            margin: 5px 0;
            border-radius: 4px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 10px 0;
        }}
        th, td {{
            padding: 8px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }}
        th {{
            background-color: #4CAF50;
            color: white;
        }}
        .expected {{
            background-color: #e8f5e9;
        }}
        .observed {{
            background-color: #fff3e0;
        }}
        .mismatch {{
            background-color: #ffebee;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Integration Test Report</h1>
        <div class="status">
            {status_emoji} Status: {status.upper()}
        </div>

        <div class="section">
            <h2>Run Information</h2>
            <p><strong>Run ID:</strong> {run_id}</p>
            <p><strong>Scenario:</strong> {metadata.get('scenario', 'N/A')}</p>
            <p><strong>Started:</strong> {metadata.get('startedAt', 'N/A')}</p>
            <p><strong>Completed:</strong> {metadata.get('completedAt', 'N/A')}</p>
        </div>

        <div class="section">
            <h2>Metrics</h2>
            <p><strong>Total Duration:</strong> {metrics.get('totalDurationMs', 'N/A')} ms</p>
            <p><strong>First Event Latency:</strong> {metrics.get('firstEventLatencyMs', 'N/A')} ms</p>
            <p><strong>Event Count:</strong> {metrics.get('eventCount', 'N/A')}</p>
        </div>

        <div class="section">
            <h2>Check Results</h2>
"""

    for check_name, check_result in checks.items():
        check_passed = check_result.get('passed', False)
        check_emoji = '✅' if check_passed else '❌'
        html += f"""
            <div class="check">
                <strong>{check_emoji} {check_name.capitalize()}:</strong> {'PASSED' if check_passed else 'FAILED'}
                <pre>{json.dumps(check_result.get('details', {}), indent=2)}</pre>
            </div>
"""

    html += """
        </div>
"""

    if failures:
        html += """
        <div class="section">
            <h2>Failures</h2>
"""
        for failure in failures:
            html += f"""
            <div class="failure">
                <strong>{failure.get('check', 'Unknown')}:</strong>
                <pre>{json.dumps(failure.get('details', {}), indent=2)}</pre>
            </div>
"""
        html += """
        </div>
"""

    html += """
        <div class="section">
            <h2>Expected vs Observed Events</h2>
            <table>
                <thead>
                    <tr>
                        <th>Type</th>
                        <th>Seq</th>
                        <th>Expected</th>
                        <th>Observed</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
"""

    # Create maps for comparison
    events_by_type_seq = {}
    for event in events:
        key = (event.get('eventType'), event.get('seq'))
        events_by_type_seq[key] = event

    for fixture in fixtures:
        event_type = fixture.get('eventType')
        seq = fixture.get('seq')
        key = (event_type, seq)
        observed = events_by_type_seq.get(key)

        match_status = '✅ Match' if observed else '❌ Missing'
        row_class = 'expected' if not observed else ('mismatch' if not passed else 'observed')

        html += f"""
                    <tr class="{row_class}">
                        <td>{event_type}</td>
                        <td>{seq}</td>
                        <td><pre>{json.dumps(fixture.get('expectedPayload', {}), indent=2)}</pre></td>
                        <td><pre>{json.dumps(observed.get('payload', {}) if observed else {}, indent=2)}</pre></td>
                        <td>{match_status}</td>
                    </tr>
"""

    html += """
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>
"""

    return html


def upload_to_s3(content: str, key: str, content_type: str = 'text/html') -> Optional[str]:
    """Upload content to S3 and return URL."""
    if not S3_BUCKET:
        return None

    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=content.encode('utf-8'),
            ContentType=content_type,
        )
        return f"https://{S3_BUCKET}.s3.amazonaws.com/{key}"
    except Exception as e:
        print(f'Error uploading to S3: {str(e)}')
        return None


def send_slack_notification(
    run_id: str,
    status: str,
    report_url: Optional[str] = None,
) -> bool:
    """Send notification to Slack."""
    if not SLACK_WEBHOOK_URL:
        return False

    passed = status == 'passed'
    color = 'good' if passed else 'danger'
    emoji = '✅' if passed else '❌'

    message = {
        'text': f'{emoji} Integration Test {status.upper()}: {run_id}',
        'attachments': [
            {
                'color': color,
                'fields': [
                    {
                        'title': 'Run ID',
                        'value': run_id,
                        'short': True,
                    },
                    {
                        'title': 'Status',
                        'value': status.upper(),
                        'short': True,
                    },
                ],
            }
        ],
    }

    if report_url:
        message['attachments'][0]['fields'].append({
            'title': 'Report',
            'value': f'<{report_url}|View Report>',
            'short': False,
        })

    try:
        response = requests.post(SLACK_WEBHOOK_URL, json=message, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f'Error sending Slack notification: {str(e)}')
        return False


# def approve_codepipeline(run_id: str, passed: bool) -> bool:
#     """Approve or reject CodePipeline stage."""
#     if not CODEPIPELINE_NAME or not passed:
#         return False

#     codepipeline = boto3.client('codepipeline')

#     try:
#         # Get pipeline execution ID from context or environment
#         # In a real implementation, this would come from the event that triggered the test
#         execution_id = os.environ.get('CODEPIPELINE_EXECUTION_ID')
#         if not execution_id:
#             print('No CodePipeline execution ID found')
#             return False

#         if passed:
#             codepipeline.put_approval_result(
#                 pipelineName=CODEPIPELINE_NAME,
#                 stageName=CODEPIPELINE_STAGE,
#                 actionName='Approval',
#                 result={
#                     'status': 'Approved',
#                     'summary': f'Integration tests passed for run {run_id}',
#                 },
#                 token=os.environ.get('CODEPIPELINE_APPROVAL_TOKEN', ''),
#             )
#         else:
#             codepipeline.put_approval_result(
#                 pipelineName=CODEPIPELINE_NAME,
#                 stageName=CODEPIPELINE_STAGE,
#                 actionName='Approval',
#                 result={
#                     'status': 'Rejected',
#                     'summary': f'Integration tests failed for run {run_id}',
#                 },
#                 token=os.environ.get('CODEPIPELINE_APPROVAL_TOKEN', ''),
#             )

#         return True
#     except Exception as e:
#         print(f'Error approving CodePipeline: {str(e)}')
#         return False


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda handler for Reporter.

    Expected event structure:
    {
        "runId": "uuid"
    }
    """
    try:
        run_id = event.get('runId')
        if not run_id:
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': 'Missing required field: runId',
                }),
            }

        # Load data
        verdict = load_verdict(run_id)
        if not verdict:
            return {
                'statusCode': 404,
                'body': json.dumps({
                    'error': f'Verdict not found for run {run_id}',
                }),
            }

        metadata = load_run_metadata(run_id)
        fixtures = load_fixtures(run_id)
        events = load_observed_events(run_id)

        if not metadata:
            return {
                'statusCode': 404,
                'body': json.dumps({
                    'error': f'Run metadata not found for {run_id}',
                }),
            }

        # Generate reports
        json_report = generate_json_report(run_id, metadata, verdict, fixtures, events)
        html_report = generate_html_report(run_id, metadata, verdict, fixtures, events)

        # Upload to S3 if configured
        report_url = None
        if S3_BUCKET:
            timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
            html_key = f"{S3_PREFIX}{run_id}_{timestamp}.html"
            json_key = f"{S3_PREFIX}{run_id}_{timestamp}.json"

            report_url = upload_to_s3(html_report, html_key, 'text/html')
            upload_to_s3(json.dumps(json_report, indent=2), json_key, 'application/json')

        # Send Slack notification
        status = verdict.get('status', 'unknown')
        send_slack_notification(run_id, status, report_url)

        # Approve/reject CodePipeline if configured
        passed = verdict.get('passed', False)
        # approve_codepipeline(run_id, passed)

        return {
            'statusCode': 200,
            'body': json.dumps({
                'runId': run_id,
                'status': status,
                'passed': passed,
                'reportUrl': report_url,
                'jsonReport': json_report,
            }),
        }

    except Exception as e:
        print(f'Error in reporter: {str(e)}')
        import traceback
        traceback.print_exc()
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': str(e),
            }),
        }
