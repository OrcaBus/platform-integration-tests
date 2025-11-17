"""
Collector Lambda Function

Listens to OrcaBus events and archives all events for a runId into DynamoDB.
Deduplicates by eventId to avoid storing the same event twice.
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3

# Initialize clients
dynamodb = boto3.resource('dynamodb')

# Environment variables
TABLE_NAME = os.environ.get('TABLE_NAME', 'platform-it-store')


def compute_payload_hash(payload: Dict[str, Any]) -> str:
    """Compute SHA256 hash of payload for idempotency checking."""
    payload_str = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(payload_str.encode()).hexdigest()


def format_sort_key(seq: int = None, timestamp: str = None) -> str:
    """
    Format sort key for event storage.
    Uses seq if available, otherwise uses timestamp.
    """
    if seq is not None:
        return f"event#seq{seq:06d}"
    elif timestamp:
        return f"event#ts#{timestamp}"
    else:
        # Fallback to current timestamp
        now = datetime.now(timezone.utc).isoformat()
        return f"event#ts#{now}"


def store_event(
    run_id: str,
    event_id: str,
    event_type: str,
    received_at: str,
    payload: Dict[str, Any],
    seq: int = None,
    caused_by: str = None,
    source: str = None,
) -> bool:
    """
    Store an event in DynamoDB with idempotency check.
    Returns True if event was stored, False if it was a duplicate.
    """
    table = dynamodb.Table(TABLE_NAME)

    # Compute payload hash
    payload_hash = compute_payload_hash(payload)

    # Format sort key
    sk = format_sort_key(seq=seq, timestamp=received_at)

    # Prepare item
    item = {
        'runId': run_id,
        'sk': sk,
        'eventId': event_id,
        'eventType': event_type,
        'receivedAt': received_at,
        'payloadHash': f"sha256:{payload_hash}",
        'payload': payload,
    }

    if seq is not None:
        item['seq'] = seq

    if caused_by:
        item['causedBy'] = caused_by
    elif source:
        item['causedBy'] = source

    # Use conditional put to ensure idempotency (only insert if eventId doesn't exist)
    try:
        table.put_item(
            Item=item,
            ConditionExpression='attribute_not_exists(eventId) OR eventId = :eventId',
            ExpressionAttributeValues={':eventId': event_id},
        )
        return True
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        # Event already exists (duplicate)
        print(f'Duplicate event detected: {event_id}')
        return False


def is_run_active(run_id: str) -> bool:
    """Check if a run is still active (not completed or timed out)."""
    table = dynamodb.Table(TABLE_NAME)

    try:
        response = table.get_item(
            Key={
                'runId': run_id,
                'sk': 'run#meta',
            }
        )

        if 'Item' not in response:
            return False

        status = response['Item'].get('status', 'unknown')
        return status == 'running'

    except Exception as e:
        print(f'Error checking run status: {str(e)}')
        return False


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda handler for Collector.

    This function is typically triggered by EventBridge rule that filters
    events from OrcaBus with testMode=true.

    Expected event structure (EventBridge event):
    {
        "source": "platform-integration-tests.seeder",
        "detail-type": "stepA.started",
        "detail": {
            "runId": "uuid",
            "scenario": "happy-path-01",
            "eventId": "uuid",
            "schemaVersion": "v1",
            "seq": 1,
            "testMode": true,
            ...
        }
    }
    """
    try:
        # Extract event detail (EventBridge format)
        if 'detail' in event:
            detail = event['detail']
        else:
            # Direct invocation format
            detail = event

        # Extract required fields
        run_id = detail.get('runId')
        event_id = detail.get('eventId')
        event_type = detail.get('eventType') or event.get('detail-type')
        test_mode = detail.get('testMode', False)

        if not run_id or not event_id:
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': 'Missing required fields: runId or eventId',
                }),
            }

        # Only process test mode events
        if not test_mode:
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'Skipping non-test event',
                }),
            }

        # Check if run is still active
        if not is_run_active(run_id):
            print(f'Run {run_id} is not active, skipping event {event_id}')
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': f'Run {run_id} is not active',
                }),
            }

        # Extract additional fields
        seq = detail.get('seq')
        source = detail.get('source')
        caused_by = detail.get('causedBy') or source
        received_at = datetime.now(timezone.utc).isoformat()

        # Extract payload (everything except metadata fields)
        metadata_fields = {
            'runId', 'scenario', 'eventId', 'schemaVersion', 'seq',
            'source', 'causedBy', 'testMode', 'eventType',
        }
        payload = {k: v for k, v in detail.items() if k not in metadata_fields}

        # Store event
        stored = store_event(
            run_id=run_id,
            event_id=event_id,
            event_type=event_type,
            received_at=received_at,
            payload=payload,
            seq=seq,
            caused_by=caused_by,
            source=source,
        )

        if stored:
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'Event stored successfully',
                    'runId': run_id,
                    'eventId': event_id,
                    'eventType': event_type,
                }),
            }
        else:
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'Duplicate event skipped',
                    'runId': run_id,
                    'eventId': event_id,
                }),
            }

    except Exception as e:
        print(f'Error in collector: {str(e)}')
        import traceback
        traceback.print_exc()
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': str(e),
            }),
        }
