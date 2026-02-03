# app/service/seeder.py
"""
Seeder Lambda Function

- Create run#meta item
- Create one slot item per fixture
- Emit initial seed event to EventBridge (testMode=True, testId=runId)
"""

import json
import logging
from typing import Optional, List, Dict, Any, Tuple
import uuid
from datetime import datetime, timedelta, timezone
import time
import random
from botocore.exceptions import ClientError
from services.utils import (
    resolve_service_name,
    get_s3_keys_for_service,
    set_nested_field,
)
from services.dynamodb import put_item_to_dynamodb
from services.s3 import get_item_from_s3, S3_BUCKET
from services.eventbridge import put_event_to_event_bus, EVENT_BUS_NAME


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _now_iso() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _load_s3_json_list(key: str) -> List[Dict[str, Any]]:
    """
    Load JSON from S3 and ensure it's a list.
    If the object does not exist, raise ClientError with NoSuchKey.
    """
    logger.info("Loading seed data from s3://%s/%s", S3_BUCKET, key)
    raw = get_item_from_s3(key)
    data = json.loads(raw)

    if isinstance(data, list):
        return data
    else:
        logger.error("Expected a JSON array in %s but got %s", key, type(data))
        raise ValueError(f"Seed file {key} must contain a JSON array")


def _load_service_seed_definitions(
    service_name: str,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Try to load events for the requested serviceName.
    If those keys don't exist, fall back to 'all'.
    Returns (events, effective_service_name).
    """
    requested_service_name = service_name
    seeds_key, _ = get_s3_keys_for_service(requested_service_name)

    try:
        seeds = _load_s3_json_list(seeds_key)
        logger.info(
            "Loaded %d seeds for serviceName=%s", len(seeds), requested_service_name
        )
        return seeds, requested_service_name
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code not in ("NoSuchKey", "NoSuchBucket"):
            logger.error(
                "Error loading seeds for serviceName=%s: %s", requested_service_name, e
            )
            raise

        # fall back to 'all'
        logger.warning(
            "Seed definitions for serviceName=%s not found, falling back to 'all'",
            requested_service_name,
        )
        seeds_key, _ = get_s3_keys_for_service("all")
        seeds = _load_s3_json_list(seeds_key)
        return seeds, "all"


def _apply_format(value: str, format_config: Optional[Dict[str, str]]) -> str:
    """
    Apply prefix and/or suffix formatting to a value based on format configuration.
    format_config can have "prefix" and/or "suffix" keys.
    """
    if not format_config:
        return value

    prefix = format_config.get("prefix", "")
    suffix = format_config.get("suffix", "")
    return f"{prefix}{value}{suffix}"


def _publish_test_events(
    instrument_run_id: str,
    service_name: str,
    events_definitions: List[Dict[str, Any]],
) -> int:
    """
    Publishes test events to EventBridge sequentially, with a delay between each
    to simulate a real service emitting a sequence of status updates over time.

    events_definitions is expected to be an array of EventBridge event objects like:
    {
      "source": "Pipe IcaEventPipeConstru-IntegrationTest",
      "detail-type": "Event from aws:sqs",
      "time": "2025-01-01T00:00:00Z",
      "detail": { ... arbitrary payload ... }
      "__replace": {
        "randomUniqueIdField": [
          {
            "name": "detail.icaEvent.id",
            "format": {
              "prefix": "r." # prefix, suffix, or both
            }
          },
        ],
        "testRunIdField": [
          "detail.icaEvent.instrumentRunId",
          "detail.icaEvent.name"
        ],
        "timeStampField": [
          "time",
          "detail.icaEvent.dateModified"
        ]
      }
    }

    Supports both lowercase (new format) and capitalized (legacy) field names.

    Dynamically replaces:
    - randomUniqueIdField with random unique id format
    - testRunIdField with test_instrument_run_id format
    - timeStampField with time and dateModified format
    """
    if not events_definitions:
        logger.info("No events to publish for serviceName=%s", service_name)
        return 0

    # Generate current timestamp (used for timeStampField)
    current_timestamp = _now_iso()

    logger.info(
        "Starting event publishing for instrumentRunId=%s, timestamp=%s",
        instrument_run_id,
        current_timestamp,
    )

    published_count = 0

    for idx, ev in enumerate(events_definitions):
        # Generate a new random unique ID for each event (used for randomUniqueIdField)
        random_unique_id = uuid.uuid4().hex

        # Deep copy the event to avoid modifying the original
        event_copy = json.loads(json.dumps(ev))

        # Extract source and detail-type (handle both lowercase and capitalized)
        source = event_copy.get("source") or event_copy.get("Source")
        detail_type = (
            event_copy.get("detail-type")
            or event_copy.get("DetailType")
            or event_copy.get("detailType")
        )

        if not source:
            logger.error("Event %d missing 'source' or 'Source' field", idx + 1)
            raise ValueError(f"Event {idx + 1} must have a 'source' field")
        if not detail_type:
            logger.error(
                "Event %d missing 'detail-type' or 'DetailType' field", idx + 1
            )
            raise ValueError(f"Event {idx + 1} must have a 'detail-type' field")

        # Extract detail (handle both lowercase and capitalized)
        detail = event_copy.get("detail") or event_copy.get("Detail", {})

        # If detail is not a dict, wrap it or use as-is
        if not isinstance(detail, dict):
            detail = {"data": detail}
            event_copy["detail"] = detail

        # Process __replace field if it exists
        replace_config = event_copy.get("__replace", {})
        if replace_config:
            # Process randomUniqueIdField
            if "randomUniqueIdField" in replace_config:
                random_fields = replace_config["randomUniqueIdField"]
                if isinstance(random_fields, list):
                    for field_config in random_fields:
                        if isinstance(field_config, dict):
                            # Object with name and optional format
                            field_path = field_config.get("name")
                            format_config = field_config.get("format")
                            if field_path:
                                value = _apply_format(random_unique_id, format_config)
                                set_nested_field(event_copy, field_path, value)
                        elif isinstance(field_config, str):
                            # Simple string path
                            value = random_unique_id
                            set_nested_field(event_copy, field_config, value)

            # Process testInstrumentRunIdField
            if "testInstrumentRunIdField" in replace_config:
                test_run_fields = replace_config["testInstrumentRunIdField"]
                if isinstance(test_run_fields, list):
                    for field_config in test_run_fields:
                        if isinstance(field_config, dict):
                            # Object with name and optional format
                            field_path = field_config.get("name")
                            format_config = field_config.get("format")
                            if field_path:
                                value = _apply_format(instrument_run_id, format_config)
                                set_nested_field(event_copy, field_path, value)
                        elif isinstance(field_config, str):
                            # Simple string path
                            value = instrument_run_id
                            set_nested_field(event_copy, field_config, value)

            # Process timeStampField
            if "timeStampField" in replace_config:
                timestamp_fields = replace_config["timeStampField"]
                if isinstance(timestamp_fields, list):
                    for field_path in timestamp_fields:
                        if isinstance(field_path, str):
                            set_nested_field(event_copy, field_path, current_timestamp)

        # Remove __replace field from the event before publishing
        event_copy.pop("__replace", None)

        # Extract detail again after replacements (in case it was modified)
        detail = event_copy.get("detail") or event_copy.get("Detail", {})

        entry = {
            "EventBusName": EVENT_BUS_NAME,
            "Source": source,
            "DetailType": detail_type,
            "Detail": json.dumps(detail),
        }

        # Include time field if it exists in the event
        if "time" in event_copy:
            entry["Time"] = event_copy["time"]

        logger.info(
            "Publishing test event %d/%d for instrumentRunId=%s, serviceName=%s (source=%s, detailType=%s)",
            idx + 1,
            len(events_definitions),
            instrument_run_id,
            service_name,
            source,
            detail_type,
        )

        put_event_to_event_bus(entry)
        published_count += 1

        # If there are more events to send, wait 1 second to simulate
        # a realistic emission interval.
        if idx < len(events_definitions) - 1:
            logger.info("Sleeping 1 second before publishing next test event")
            time.sleep(1)

    logger.info(
        "Published %d test events to EventBridge for instrumentRunId=%s, serviceName=%s",
        published_count,
        instrument_run_id,
        service_name,
    )
    return published_count


def _generate_test_instrument_run_id(service_name_abbreviation: str) -> str:
    """
    Generate a test run id (instrumentRunId) in the format
    date in format YYMMDD_A00001_XXXX_TESTSRMXXX
    X will be randomly generated from 0 to 9 in string format
    """
    # Generate random number from 0001 to 9999
    random_number_str_4digits = random.randint(1, 9999)
    random_number_str_3digits = random.randint(1, 999)

    return f"{datetime.now(tz=timezone.utc).strftime('%y%m%d')}_A00001_{random_number_str_4digits:04d}_TEST{service_name_abbreviation}{random_number_str_3digits:03d}"


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
    - Emit seed events to EventBridge (testMode=true, testId=runId)
    """
    logger.info(f"[Seeder] Event: {json.dumps(event)}")

    # retrieve payload from event
    payload = event.get("Payload") or event.get("payload")
    if not payload:
        logger.error("payload is required")
        raise ValueError("payload is required")
    raw_service_name = payload.get("serviceName", "all")
    requested_service_name, service_name_abbreviation = resolve_service_name(
        raw_service_name
    )

    # random generate test instrument run id for the test run
    test_instrument_run_id = _generate_test_instrument_run_id(service_name_abbreviation)

    logger.info(
        "Starting seeding for testInstrumentRunId=%s, requestedServiceName=%s",
        test_instrument_run_id,
        requested_service_name,
    )

    try:
        events_defs, effective_service_name = _load_service_seed_definitions(
            requested_service_name
        )
    except ClientError as e:
        logger.error(
            "Error loading seed definitions for serviceName=%s: %s",
            requested_service_name,
            e,
        )
        raise

    now = datetime.now(tz=timezone.utc)
    started_at = _now_iso()
    # Create timeout_at in 5 minutes from now in ISO format ending with Z (UTC)
    timeout_at = (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Create run meta item FIRST to ensure it exists before events are published
    # This prevents the collector from ignoring events due to missing run meta
    meta_item = {
        "testId": f"run#{test_instrument_run_id}",
        "sk": "run#meta",
        "serviceName": effective_service_name,
        "observedCount": 0,
        "status": "running",
        "startedAt": started_at,
        "timeoutAt": timeout_at,
    }
    put_item_to_dynamodb(meta_item)
    logger.info(
        f"[Seeder] Created run meta for testInstrumentRunId={test_instrument_run_id}"
    )

    # # 2. create all expected events item in dynamo db
    # for event_def in events_defs:
    #     event_id = uuid.uuid4().hex
    #     event_item = {
    #         "testId": f"run#{test_instrument_run_id}",
    #         "sk": f"event#{event_id}",
    #         "eventId": event_id,
    #     }
    #     table.put_item(Item=event_item)

    # 3. Publish test events to EventBridge AFTER meta item is created
    published_count = _publish_test_events(
        test_instrument_run_id, effective_service_name, events_defs
    )

    return {
        "testInstrumentRunId": test_instrument_run_id,
        "serviceName": effective_service_name,
        "startedAt": started_at,
        "timeoutAt": timeout_at,
        "publishedCount": published_count,
    }
