# app/service/seeder.py
"""
Seeder Lambda Function

- Create run#meta item
- Create one slot item per fixture
- Emit initial seed event to EventBridge (testMode=True, testId=runId)
"""

import os
import json
import uuid
from datetime import datetime, timedelta

import boto3

TABLE_NAME = os.environ["TABLE_NAME"]
EVENT_BUS_NAME = os.environ["EVENT_BUS_NAME"]
S3_BUCKET = os.environ["S3_BUCKET"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
events_client = boto3.client("events")
s3 = boto3.client("s3")


def _now_iso() -> str:
    return (
        datetime.now(tz=datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _load_fixtures_from_s3(scenario: str):
    """
    Try to load fixtures from S3 at key: fixtures/<scenario>.json

    Expected format:
    [
      {
        "detailType": "...",
        "source": "...",
        "payloadHash": "...",  # optional
        "rawS3Key": "...",     # optional
        ...
      },
      ...
    ]
    """
    key = f"fixtures/{scenario}.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        body = obj["Body"].read()
        fixtures = json.loads(body)
        return fixtures
    except s3.exceptions.NoSuchKey:
        print(
            f"[Seeder] No fixtures found at s3://{S3_BUCKET}/{key}, using inline sample."
        )
        return None
    except Exception as e:
        print(f"[Seeder] Error loading fixtures from S3: {e}")
        return None


def _inline_sample_fixtures():
    """
    Very simple sample fixtures for local/dev.
    Replace with your own or rely on S3-based fixtures.
    """
    return [
        {
            "detailType": "orcabus.sample.step1",
            "source": "orcabus.integration.test",
            "payloadHash": None,
            "rawS3Key": None,
        },
        {
            "detailType": "orcabus.sample.step2",
            "source": "orcabus.integration.test",
            "payloadHash": None,
            "rawS3Key": None,
        },
    ]


def handler(event, context):
    """
    Expected Step Functions input:
    {
      "runId": "<uuid or pipeline-provided>",
      "scenario": "daily-batch-orchestration",
      ... (other fields ignored)
    }

    Seeder will:
    - Create run#meta item
    - Create one slot item per fixture
    - Emit initial seed event to EventBridge (testMode=true, testId=runId)
    """
    print(f"[Seeder] Event: {json.dumps(event)}")

    run_id = event.get("runId") or str(uuid.uuid4())
    scenario = event.get("scenario", "default-scenario")

    # 1. Load fixtures (S3 first, then inline fallback)
    fixtures = _load_fixtures_from_s3(scenario)
    if fixtures is None:
        fixtures = _inline_sample_fixtures()

    expected_slots = len(fixtures)
    now = datetime.utcnow()
    started_at = _now_iso()
    timeout_at = (now + timedelta(minutes=2)).isoformat(timespec="seconds") + "Z"

    # 2. Create run meta item
    meta_item = {
        "pk": f"run#{run_id}",
        "sk": "run#meta",
        "runId": run_id,
        "scenario": scenario,
        "expectedSlots": expected_slots,
        "observedCount": 0,
        "status": "running",
        "startedAt": started_at,
        "timeoutAt": timeout_at,
    }
    table.put_item(Item=meta_item)
    print(f"[Seeder] Created run meta for {run_id}")

    # 3. Create slot items
    with table.batch_writer() as batch:
        for idx, fixture in enumerate(fixtures, start=1):
            slot_item = {
                "pk": f"run#{run_id}",
                "sk": f"slot#seq#{idx:06d}",
                "runId": run_id,
                "slotType": "seq",
                "slotId": idx,
                "expected": fixture,
                "observedEvents": [],
                "verdict": {
                    "status": "pending",
                    "reasons": [],
                },
            }
            batch.put_item(Item=slot_item)
    print(f"[Seeder] Wrote {expected_slots} slot items for run {run_id}")

    # 4. Emit seed event to EventBridge (testMode=true)
    detail = {
        "testMode": True,
        "testId": run_id,
        "scenario": scenario,
    }
    events_client.put_events(
        Entries=[
            {
                "Source": "orcabus.integration.test-harness",
                "DetailType": "orcabus.integration.seed",
                "EventBusName": EVENT_BUS_NAME,
                "Detail": json.dumps(detail),
            }
        ]
    )
    print(f"[Seeder] Sent seed event for run {run_id} to bus {EVENT_BUS_NAME}")

    return {
        "runId": run_id,
        "scenario": scenario,
        "expectedSlots": expected_slots,
        "startedAt": started_at,
        "timeoutAt": timeout_at,
    }
