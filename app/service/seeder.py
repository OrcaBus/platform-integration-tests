"""
Seeder Lambda Function

Publishes scenario seed events to the OrcaBus staging test bus.
Each event is tagged with runId, eventId, schemaVersion, and seq (strict order).
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3

# Initialize clients
eventbridge = boto3.client('events')
dynamodb = boto3.resource('dynamodb')

# Environment variables
TABLE_NAME = os.environ.get('TABLE_NAME', 'platform-it-store')
ORCABUS_BUS_NAME = os.environ.get('ORCABUS_BUS_NAME', 'staging-test-bus')
SCHEMA_VERSION = os.environ.get('SCHEMA_VERSION', 'v1')


def generate_run_id() -> str:
    """Generate a unique run ID."""
    return str(uuid.uuid4())


def generate_event_id() -> str:
    """Generate a unique event ID."""
    return str(uuid.uuid4())


def load_scenario_fixtures(scenario: str) -> List[Dict[str, Any]]:
    """
    Load expected fixtures for a scenario.
    In a real implementation, this might load from a fixtures file or DynamoDB.
    For now, returns a simple example scenario.
    """
    # Example scenarios - in production, load from fixtures directory or DynamoDB
    scenarios = {
        'happy-path-01': [
            {
                'eventType': 'stepA.started',
                'seq': 1,
                'payload': {'action': 'start', 'step': 'A'},
            },
            {
                'eventType': 'stepA.completed',
                'seq': 2,
                'payload': {'action': 'complete', 'step': 'A', 'result': 'success'},
            },
            {
                'eventType': 'stepB.started',
                'seq': 3,
                'payload': {'action': 'start', 'step': 'B'},
            },
            {
                'eventType': 'stepB.completed',
                'seq': 4,
                'payload': {'action': 'complete', 'step': 'B', 'result': 'success'},
            },
        ],
    }

    return scenarios.get(scenario, [])


def store_run_metadata(run_id: str, scenario: str, expected_count: int, timeout_seconds: int = 300):
    """Store run metadata in DynamoDB."""
    table = dynamodb.Table(TABLE_NAME)
    now = datetime.now(timezone.utc).isoformat()
    timeout_at = (datetime.now(timezone.utc).timestamp() + timeout_seconds).isoformat()

    table.put_item(
        Item={
            'runId': run_id,
            'sk': 'run#meta',
            'scenario': scenario,
            'expectedCount': expected_count,
            'status': 'running',
            'startedAt': now,
            'timeoutAt': timeout_at,
            'ttl': int(datetime.now(timezone.utc).timestamp()) + (timeout_seconds * 2),  # Cleanup after 2x timeout
        }
    )


def store_fixtures(run_id: str, scenario: str, fixtures: List[Dict[str, Any]]):
    """Store expected fixtures in DynamoDB."""
    table = dynamodb.Table(TABLE_NAME)

    for idx, fixture in enumerate(fixtures):
        table.put_item(
            Item={
                'runId': run_id,
                'sk': f"fixture#{fixture['eventType']}#{idx}",
                'eventType': fixture['eventType'],
                'seq': fixture.get('seq'),
                'expectedPayload': fixture.get('payload', {}),
                'scenario': scenario,
            }
        )


def publish_event_to_orcabus(
    run_id: str,
    event_id: str,
    scenario: str,
    event_type: str,
    seq: int,
    source: str = None,
    payload: Dict[str, Any] = None,
) -> None:
    """
    Publish an event to OrcaBus (EventBridge custom bus).
    """
    event_detail = {
        'runId': run_id,
        'scenario': scenario,
        'eventId': event_id,
        'schemaVersion': SCHEMA_VERSION,
        'seq': seq,
        'testMode': True,
        'eventType': event_type,
    }

    if source:
        event_detail['source'] = source

    if payload:
        event_detail.update(payload)

    # Publish to EventBridge custom bus
    eventbridge.put_events(
        Entries=[
            {
                'Source': 'platform-integration-tests.seeder',
                'DetailType': event_type,
                'Detail': json.dumps(event_detail),
                'EventBusName': ORCABUS_BUS_NAME,
            }
        ]
    )


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda handler for Seeder.

    Expected event structure:
    {
        "scenario": "happy-path-01",
        "timeoutSeconds": 300  # optional, default 300
    }
    """
    try:
        # Extract parameters
        scenario = event.get('scenario', 'happy-path-01')
        timeout_seconds = event.get('timeoutSeconds', 300)

        # Generate run ID
        run_id = generate_run_id()

        # Load scenario fixtures
        fixtures = load_scenario_fixtures(scenario)
        if not fixtures:
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': f'Scenario "{scenario}" not found',
                    'runId': run_id,
                }),
            }

        expected_count = len(fixtures)

        # Store run metadata
        store_run_metadata(run_id, scenario, expected_count, timeout_seconds)

        # Store fixtures
        store_fixtures(run_id, scenario, fixtures)

        # Publish events to OrcaBus
        previous_event_id = None
        published_events = []

        for fixture in fixtures:
            event_id = generate_event_id()
            seq = fixture.get('seq', 0)
            event_type = fixture['eventType']
            payload = fixture.get('payload', {})

            # Publish to OrcaBus
            publish_event_to_orcabus(
                run_id=run_id,
                event_id=event_id,
                scenario=scenario,
                event_type=event_type,
                seq=seq,
                source=previous_event_id,
                payload=payload,
            )

            published_events.append({
                'eventId': event_id,
                'eventType': event_type,
                'seq': seq,
            })

            previous_event_id = event_id

        return {
            'statusCode': 200,
            'body': json.dumps({
                'runId': run_id,
                'scenario': scenario,
                'expectedCount': expected_count,
                'publishedEvents': published_events,
                'message': f'Successfully seeded {len(published_events)} events',
            }),
        }

    except Exception as e:
        print(f'Error in seeder: {str(e)}')
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': str(e),
            }),
        }
