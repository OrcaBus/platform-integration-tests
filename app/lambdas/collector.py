# app/service/collector.py
"""
Collector

Triggered by EventBridge rule.

EventBridge sends events that include:

  test event id filed (define by config.TEST_ID_MAPPING)

Collector:
  - Extracts test_id from event details.
  - Ignores events without test_id (not part of an integration test run).
  - Loads run meta (run#meta) to ensure the run exists.
  - Stores the full EventBridge event into S3 using a time-based path.
  - Writes observed event record to DynamoDB with:
    - pk: run#{test_id}
    - sk: event#{timestamp}
    - detailType, source, payloadHash, rawS3Key, receivedAt

This keeps Collector lightweight and fast - just raw archival.
No matching logic, no status updates, no knowledge of expectations.
"""

import json
from datetime import datetime, timezone

import logging
from utils.utils import utc_timestamp, get_event_test_id, sanitize_string
from services.aws.dynamodb import get_run_meta, put_item_to_dynamodb
from services.aws.s3 import store_item_to_s3, S3_BUCKET

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _store_event_payload(full_event: dict) -> str:
    f"""
    Store the full EventBridge event in S3 and return the key.

    Path layout (time-based hierarchy):

      events/testruns/year=YYYY/month=MM/day=DD/testId/event_detail_type_timestamp.json
    """
    now = datetime.now(timezone.utc)
    yyyy = now.strftime("%Y")
    mm = now.strftime("%m")
    dd = now.strftime("%d")

    event_detail_type = (
        full_event.get("detail-type") or full_event.get("DetailType") or "unknown"
    )

    test_id = get_event_test_id(full_event)
    if not test_id:
        logger.error(
            f"[Collector] No test id found for event: {json.dumps(full_event)}"
        )
        raise ValueError(f"No test id found for event: {json.dumps(full_event)}")

    # Sanitize detail-type and timestamp for use in S3 key (replace special characters)
    event_detail_type_safe = sanitize_string(event_detail_type)
    time_stamp = str(datetime.now(timezone.utc).timestamp())  # for object name

    key = f"events/testruns/year={yyyy}/month={mm}/day={dd}/{test_id}/{event_detail_type_safe}_{time_stamp}.json"

    try:
        store_item_to_s3(key, json.dumps(full_event))
        return key
    except Exception as e:
        logger.error(f"[Collector] Failed to store event payload to S3: {e}")
        raise e


def handler(event, context):
    """
    Collector  will consume all events from the EventBridge bus.
    EventBridge event shape (simplified):
    {
        "id": "...",
        "source": "...",
        "detail-type": "...",
        "detail": {
        "testId": "...",
        },
        ...
    }
    Example EventBridge event:
    {
    "version": "0",
    "id": "7fff8d7d-fed8-b38f-2c0b-c843b0e194e2",
    "detail-type": "service", exclude Event from aws:sqs event
    "source": "Pipe IcaEventPipeConstru-IntegrationTest",
    "account": "455634345446",
    "time": "2026-01-22T02:11:48Z",
    "region": "ap-southeast-2",
    "resources": [],
    "detail": {
        "instrumentRunId": "260122_A00001_1234_TEST123456",
        ...
    }
    """
    logger.info(f"[Collector] EventBridge event: {json.dumps(event)}")
    # Check if it is a event from details.detail-type: "Event from aws:sqs", if yes, ignore the event as it is seed events
    # if no, continue with the event
    seeder_events_source = ["orcabus.integrationtests", "orcabus.integrationtests.seed"]

    # step 1: check if the event is a seed event
    if event.get("source") in seeder_events_source:
        logger.info("[Collector] Event is a seed event, ignoring.")
        return {"ignored": True, "reason": "seed_event from IntegrationTest seeder."}

    # step 2: get the instrument run id from the event
    test_id = get_event_test_id(event) or None
    if not test_id:
        logger.info(f"[Collector] No test id found for event, ignoring event.")
        return {"ignored": True, "reason": "no test id found for event."}

    # step 3: get the run meta from the database
    run_meta = get_run_meta(test_id)
    if not run_meta:
        logger.info(
            f"[Collector] No run meta found for testId={test_id}, ignoring event."
        )
        return {
            "ignored": True,
            "reason": "no_run_meta",
            "testId": test_id,
        }
    logger.info(f"[Collector] Successfully found run meta for testId={test_id}")

    # step 4: store the event in the database
    detail_type = event.get("detail-type", "")
    source = event.get("source", "")

    received_at = utc_timestamp()
    sk = f"event#{str(datetime.now(timezone.utc).timestamp())}"

    # TODO: store the event in the database for now, instead plan for store event in S3 later and store hash and s3 key in the database
    event_item = {
        "testId": test_id,
        "sk": sk,
        "detailType": detail_type,
        "source": source,
        "observedEvent": event,
        "receivedAt": received_at,
    }

    # payload_hash = hash_payload(event.get("detail"))
    # s3_key = _store_event_payload(event)
    # event_item = {
    #     "testId": test_id,
    #     "sk": sk,
    #     "detailType": detail_type,
    #     "source": source,
    #     "payloadHash": payload_hash or None,
    #     "rawS3Key": s3_key or None,
    #     "receivedAt": received_at,
    # }

    try:
        put_item_to_dynamodb(event_item)
        logger.info(
            f"[Collector] Stored event record for testId={test_id}, "
            f"detailType={detail_type}, source={source}"
        )
    except Exception as e:
        logger.error(f"[Collector] Failed to store event record: {e}")
        raise e

    return {
        "testId": test_id,
        "stored": True,
        "eventKey": {"testId": test_id, "sk": sk},
    }
