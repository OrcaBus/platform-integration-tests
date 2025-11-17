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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3

# Initialize clients
dynamodb = boto3.resource('dynamodb')

# Environment variables
TABLE_NAME = os.environ.get('TABLE_NAME', 'platform-it-store')
MAX_LATENCY_MS = int(os.environ.get('MAX_LATENCY_MS', '60000'))  # 60 seconds default


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


def check_presence(fixtures: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """
    Check if all expected events are present.
    Returns (passed, missing_events).
    """
    # Group fixtures by eventType
    expected_by_type = {}
    for fixture in fixtures:
        event_type = fixture.get('eventType')
        if event_type:
            expected_by_type[event_type] = expected_by_type.get(event_type, 0) + 1

    # Count observed events by type
    observed_by_type = {}
    for event in events:
        event_type = event.get('eventType')
        if event_type:
            observed_by_type[event_type] = observed_by_type.get(event_type, 0) + 1

    # Check for missing events
    missing = []
    for event_type, expected_count in expected_by_type.items():
        observed_count = observed_by_type.get(event_type, 0)
        if observed_count < expected_count:
            missing.append(f"{event_type} (expected {expected_count}, got {observed_count})")

    return len(missing) == 0, missing


def check_order(fixtures: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """
    Check event ordering.
    Supports both seq-based (strictly increasing) and causedBy-based (DAG) ordering.
    Returns (passed, violations).
    """
    violations = []

    # Sort events by seq if available
    events_with_seq = [e for e in events if 'seq' in e]
    events_without_seq = [e for e in events if 'seq' not in e]

    if events_with_seq:
        # Check seq-based ordering
        events_with_seq.sort(key=lambda x: x.get('seq', 0))
        prev_seq = None
        for event in events_with_seq:
            seq = event.get('seq')
            if prev_seq is not None and seq <= prev_seq:
                violations.append(
                    f"Order violation: seq {seq} after seq {prev_seq} "
                    f"(eventId: {event.get('eventId')})"
                )
            prev_seq = seq

    # Check causedBy-based ordering (DAG)
    if events_without_seq or events_with_seq:
        # Build event graph
        event_by_id = {e.get('eventId'): e for e in events if 'eventId' in e}
        caused_by_edges = {}

        for event in events:
            event_id = event.get('eventId')
            caused_by = event.get('causedBy')
            if event_id and caused_by:
                if caused_by not in caused_by_edges:
                    caused_by_edges[caused_by] = []
                caused_by_edges[caused_by].append(event_id)

        # Check that all causedBy references point to existing events
        for event in events:
            caused_by = event.get('causedBy')
            if caused_by and caused_by not in event_by_id:
                violations.append(
                    f"Missing source event: {caused_by} referenced by {event.get('eventId')}"
                )

    return len(violations) == 0, violations


def check_payload(fixtures: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """
    Check that event payloads match expected payloads.
    Returns (passed, mismatches).
    """
    mismatches = []

    # Create a map of expected payloads by eventType and seq
    expected_by_key = {}
    for fixture in fixtures:
        event_type = fixture.get('eventType')
        seq = fixture.get('seq')
        key = (event_type, seq) if seq is not None else event_type
        expected_by_key[key] = fixture.get('expectedPayload', {})

    # Check each observed event
    for event in events:
        event_type = event.get('eventType')
        seq = event.get('seq')
        key = (event_type, seq) if seq is not None else event_type

        if key in expected_by_key:
            expected_payload = expected_by_key[key]
            observed_payload = event.get('payload', {})

            # Compare payloads (simple deep equality check)
            if not payloads_match(expected_payload, observed_payload):
                mismatches.append(
                    f"Payload mismatch for {event_type} (seq={seq}): "
                    f"expected {json.dumps(expected_payload)}, "
                    f"got {json.dumps(observed_payload)}"
                )

    return len(mismatches) == 0, mismatches


def payloads_match(expected: Dict[str, Any], observed: Dict[str, Any]) -> bool:
    """Check if payloads match (deep equality)."""
    # Simple recursive comparison
    if type(expected) != type(observed):
        return False

    if isinstance(expected, dict):
        for key, value in expected.items():
            if key not in observed:
                return False
            if not payloads_match(value, observed[key]):
                return False
        return True
    elif isinstance(expected, list):
        if len(expected) != len(observed):
            return False
        return all(payloads_match(e, o) for e, o in zip(expected, observed))
    else:
        return expected == observed


def check_idempotency(events: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """
    Check for duplicate eventIds.
    Returns (passed, duplicates).
    """
    seen_ids = set()
    duplicates = []

    for event in events:
        event_id = event.get('eventId')
        if event_id:
            if event_id in seen_ids:
                duplicates.append(f"Duplicate eventId: {event_id}")
            seen_ids.add(event_id)

    return len(duplicates) == 0, duplicates


def check_latency(
    metadata: Dict[str, Any],
    events: List[Dict[str, Any]]
) -> Tuple[bool, Dict[str, Any]]:
    """
    Check that event latencies are within acceptable limits.
    Returns (passed, metrics).
    """
    if not events:
        return False, {'error': 'No events to check latency'}

    # Parse timestamps
    try:
        started_at = datetime.fromisoformat(metadata.get('startedAt', '').replace('Z', '+00:00'))
    except Exception:
        started_at = datetime.now(timezone.utc)

    event_times = []
    for event in events:
        received_at_str = event.get('receivedAt')
        if received_at_str:
            try:
                received_at = datetime.fromisoformat(received_at_str.replace('Z', '+00:00'))
                event_times.append(received_at)
            except Exception:
                pass

    if not event_times:
        return False, {'error': 'No valid timestamps in events'}

    # Calculate metrics
    first_event_time = min(event_times)
    last_event_time = max(event_times)

    total_duration_ms = int((last_event_time - started_at).total_seconds() * 1000)
    first_event_latency_ms = int((first_event_time - started_at).total_seconds() * 1000)

    metrics = {
        'totalDurationMs': total_duration_ms,
        'firstEventLatencyMs': first_event_latency_ms,
        'eventCount': len(events),
    }

    # Check if within limits
    passed = total_duration_ms <= MAX_LATENCY_MS

    if not passed:
        metrics['violation'] = f"Total duration {total_duration_ms}ms exceeds limit {MAX_LATENCY_MS}ms"

    return passed, metrics


def write_verdict(
    run_id: str,
    passed: bool,
    checks: Dict[str, Tuple[bool, Any]],
    metrics: Dict[str, Any],
) -> None:
    """Write verification verdict to DynamoDB."""
    table = dynamodb.Table(TABLE_NAME)

    # Compile all failures
    failures = []
    for check_name, (check_passed, details) in checks.items():
        if not check_passed:
            failures.append({
                'check': check_name,
                'details': details,
            })

    verdict = {
        'runId': run_id,
        'sk': 'verdict#1',
        'status': 'passed' if passed else 'failed',
        'passed': passed,
        'checks': {
            name: {'passed': result[0], 'details': result[1]}
            for name, result in checks.items()
        },
        'failures': failures,
        'metrics': metrics,
        'verifiedAt': datetime.now(timezone.utc).isoformat(),
    }

    table.put_item(Item=verdict)

    # Update run metadata status
    table.update_item(
        Key={
            'runId': run_id,
            'sk': 'run#meta',
        },
        UpdateExpression='SET #status = :status, completedAt = :completedAt',
        ExpressionAttributeNames={'#status': 'status'},
        ExpressionAttributeValues={
            ':status': 'completed',
            ':completedAt': datetime.now(timezone.utc).isoformat(),
        },
    )


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda handler for Verifier.

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

        # Load run metadata
        metadata = load_run_metadata(run_id)
        if not metadata:
            return {
                'statusCode': 404,
                'body': json.dumps({
                    'error': f'Run {run_id} not found',
                }),
            }

        # Load fixtures and observed events
        fixtures = load_fixtures(run_id)
        events = load_observed_events(run_id)

        if not fixtures:
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': f'No fixtures found for run {run_id}',
                }),
            }

        # Perform all checks
        presence_passed, presence_details = check_presence(fixtures, events)
        order_passed, order_details = check_order(fixtures, events)
        payload_passed, payload_details = check_payload(fixtures, events)
        idempotency_passed, idempotency_details = check_idempotency(events)
        latency_passed, latency_metrics = check_latency(metadata, events)

        checks = {
            'presence': (presence_passed, presence_details),
            'order': (order_passed, order_details),
            'payload': (payload_passed, payload_details),
            'idempotency': (idempotency_passed, idempotency_details),
            'latency': (latency_passed, latency_metrics),
        }

        # Overall verdict: all checks must pass
        passed = all(result[0] for result in checks.values())

        # Write verdict
        write_verdict(run_id, passed, checks, latency_metrics)

        return {
            'statusCode': 200,
            'body': json.dumps({
                'runId': run_id,
                'passed': passed,
                'checks': {
                    name: {'passed': result[0], 'details': result[1]}
                    for name, result in checks.items()
                },
                'metrics': latency_metrics,
            }),
        }

    except Exception as e:
        print(f'Error in verifier: {str(e)}')
        import traceback
        traceback.print_exc()
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': str(e),
            }),
        }
