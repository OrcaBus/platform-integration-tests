import json
import logging
from typing import List, Dict, Any
import time
from datetime import datetime, timezone, timedelta
import random
import uuid
from utils.utils import (
    utc_timestamp,
    set_nested_field,
    apply_format,
    process_expectation_replace_fields,
    execute_event_comparison,
    parse_iso_safe,
)
from utils.config import SERVICE_ABBREVIATIONS
from services.aws.s3 import (
    load_service_seed_definitions,
    load_s3_json_list,
    load_service_expectations,
)
from services.aws.dynamodb import (
    put_item_to_dynamodb,
    get_observed_events,
    get_run_meta,
    update_item_to_dynamodb,
    get_items_from_dynamodb,
)
from services.aws.eventbridge import put_event_to_event_bus, EVENT_BUS_NAME
from services.base_service import BaseService

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class SequenceRunManagerService(BaseService):
    def __init__(self, service_name: str = "sequencerunmanager"):
        super().__init__(service_name)

    def generate_seed_instrument_run_id(self) -> str:
        """
        Generate a seed instrument run id in the format
        date in format YYMMDD_A00001_XXXX_TESTSRMXXX
        X will be randomly generated from 0 to 9 in string format
        """
        service_name_abbreviation = SERVICE_ABBREVIATIONS[self.service_name]
        return f"{datetime.now(tz=timezone.utc).strftime('%y%m%d')}_A00001_{random.randint(1, 9999):04d}_TEST{service_name_abbreviation}{random.randint(1, 999):03d}"

    def publish_srm_seed_events(
        instrument_run_id: str,
        sequence_run_id: str,
        service_name: str,
        seed_definitions: List[Dict[str, Any]],
    ) -> int:
        """
        Publishes seed events to EventBridge sequentially, with a delay between each
        to simulate a real service emitting a sequence of status updates over time.

        seed_definitions is expected to be an array of EventBridge event objects like:
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
        if not seed_definitions:
            logger.info("No events to publish for serviceName=%s", service_name)
            return 0

        # Generate current timestamp (used for timeStampField)
        current_timestamp = utc_timestamp()

        logger.info(
            "Starting event publishing for instrumentRunId=%s, timestamp=%s",
            instrument_run_id,
            current_timestamp,
        )

        published_count = 0

        for idx, seed in enumerate(seed_definitions):
            # Generate a new random unique ID for each event (used for randomUniqueIdField)
            random_unique_id = sequence_run_id

            # Deep copy the event to avoid modifying the original
            event_copy = json.loads(json.dumps(seed))

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
                                    value = apply_format(
                                        random_unique_id, format_config
                                    )
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
                                    value = apply_format(
                                        instrument_run_id, format_config
                                    )
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
                                set_nested_field(
                                    event_copy, field_path, current_timestamp
                                )

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
                len(seed_definitions),
                instrument_run_id,
                service_name,
                source,
                detail_type,
            )

            put_event_to_event_bus(entry)
            published_count += 1

            # If there are more events to send, wait 1 second to simulate
            # a realistic emission interval.
            if idx < len(seed_definitions) - 1:
                logger.info("Sleeping 1 second before publishing next test event")
                time.sleep(1)

        logger.info(
            "Published %d test events to EventBridge for instrumentRunId=%s, serviceName=%s",
            published_count,
            instrument_run_id,
            service_name,
        )
        return published_count

    def execute_seed_process(self) -> Dict[str, Any]:
        """
        Seed process for sequence run manager service
        - Generate a unique seed instrument run id
        - Generate a random unique id for the seed run
        - Load seed definitions from S3 for the given service name
        - Create a run meta item in DynamoDB
        - Publish seed events to EventBridge
        - Return the test instrument run id, service name, started at, timeout at, and published count
        """
        # random generate test instrument run id for the test run
        seed_instrument_run_id = self.generate_seed_instrument_run_id()
        # generate a random unique id for the test run
        seed_sequence_run_id = uuid.uuid4().hex

        logger.info(
            "Starting seeding for testInstrumentRunId=%s, requestedServiceName=%s",
            seed_instrument_run_id,
            self.service_name,
        )

        try:
            seed_definitions = load_service_seed_definitions(
                service_name=self.service_name
            )
        except Exception as e:
            logger.error(
                f"Error loading seed definitions for serviceName={self.service_name}: {e}",
                exc_info=True,
            )
            raise

        started_at = utc_timestamp()
        # Create timeout_at in 5 minutes from now in ISO format ending with Z (UTC)
        timeout_at = utc_timestamp(extend_minutes=5)

        # Publish seed events to EventBridge
        published_count = self.publish_srm_seed_events(
            seed_instrument_run_id,
            seed_sequence_run_id,
            self.service_name,
            seed_definitions,
        )

        return {
            "seedInstrumentRunId": seed_instrument_run_id,
            "serviceName": self.service_name,
            "startedAt": started_at,
            "timeoutAt": timeout_at,
            "publishedEventsCount": published_count,
        }

    def execute_verify_process(self, seed_result: Dict[str, str]) -> Dict[str, Any]:
        """
        Execute the verification process for this service.

        Verify mode: Load expectations, match against observed events, write results.

        Args:
            test_instrument_run_id: The test instrument run ID (format: "YYMMDD_A00001_XXXX_TESTXXXXXX")
        Returns:
        {
            "runId": test_instrument_run_id,
            "runStatus": "passed|failed",
            "matchedEventsCount": N,
            "missingEventsCount": N,
            "unexpectedEventsCount": N,
            "totalExpected": N
        }
        """
        seed_instrument_run_id = seed_result.get("seedInstrumentRunId")
        meta = get_run_meta(seed_instrument_run_id)
        if not meta:
            logger.error(
                f"No run meta found for testInstrumentRunId={seed_instrument_run_id}"
            )
            raise ValueError(
                f"No run meta found for testInstrumentRunId={seed_instrument_run_id}"
            )

        expectations = load_service_expectations(self.service_name)

        verifier_at = utc_timestamp()
        matched_count = 0
        missing_count = 0
        matched_event_keys = []  # Track which events were matched

        # Process each expected event in order
        for idx, expected in enumerate(expectations):
            # Process __replace fields first to get the processed expectation
            processed_expected = process_expectation_replace_fields(
                expected, seed_instrument_run_id
            )

            detail_type = processed_expected.get("detail-type") or expected.get(
                "detail-type"
            )
            source = processed_expected.get("source") or expected.get("source")
            match_fields = expected.get("__match", {}).get("fields", [])

            if not detail_type or not source:
                print(
                    f"[Verifier/Verify] Skipping expectation {idx}: missing detail-type or source"
                )
                continue

            # Query for matching observed events
            observed_events = get_observed_events(
                seed_instrument_run_id, detail_type, source
            )

            # Find matching event using processed expectation
            matched_event = execute_event_comparison(
                processed_expected, observed_events, match_fields
            )

            if matched_event:
                # Write match info to DynamoDB
                matched_count += 1
                event_key = {
                    "testId": matched_event["testId"],
                    "sk": matched_event["sk"],
                }
                matched_event_keys.append(event_key)
                try:
                    update_item_to_dynamodb(
                        key=event_key,
                        UpdateExpression="SET #s = :status, verifiedAt = :verifiedAt, expectedOrder = :order, expectedEvent = :expected",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={
                            ":status": "matched",
                            ":verifiedAt": verifier_at,
                            ":order": idx,
                            ":expected": processed_expected,
                        },
                    )
                except Exception as e:
                    print(f"[Verifier/Verify] Failed to update matched event: {e}")
                    raise e
            else:
                # Write missing event item to DynamoDB
                missing_count += 1
                missing_sk = f"event#{idx:03d}-missing"

                missing_item = {
                    "testId": f"run#{seed_instrument_run_id}",
                    "sk": missing_sk,
                    "detailType": detail_type,
                    "source": source,
                    "expectedEvent": processed_expected,
                    "status": "missed",
                    "verifiedAt": verifier_at,
                    "expectedOrder": idx,
                }
                put_item_to_dynamodb(missing_item)

        # Check for unexpected events (events not matched to any expectation)
        unexpected_count = 0
        try:
            all_observed_events = get_observed_events(seed_instrument_run_id, "", "")

            # Check each observed event to see if it was matched
            for event_item in all_observed_events:
                if event_item["sk"] not in [
                    event["sk"] for event in matched_event_keys
                ]:
                    # This event was not matched to any expectation
                    unexpected_count += 1
                    try:
                        update_item_to_dynamodb(
                            key={
                                "testId": event_item["testId"],
                                "sk": event_item["sk"],
                            },
                            UpdateExpression="SET #s = :status, verifiedAt = :verifiedAt",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={
                                ":status": "unexpected",
                                ":verifiedAt": verifier_at,
                            },
                        )
                    except Exception as e:
                        print(
                            f"[Verifier/Verify] Failed to mark event as unexpected: {e}"
                        )
        except Exception as e:
            print(f"[Verifier/Verify] Failed to check for unexpected events: {e}")

        # Determine run status
        current_status = meta.get("status", "running")
        if current_status == "timeout":
            run_status = "failed"
        elif missing_count > 0 or unexpected_count > 0:
            run_status = "failed"
        else:
            run_status = "passed"

        # Update run meta status
        try:
            update_item_to_dynamodb(
                Key={"testId": meta["testId"], "sk": meta["sk"]},
                UpdateExpression="SET #s = :status, verifiedAt = :verifiedAt",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":status": run_status,
                    ":verifiedAt": verifier_at,
                },
            )
        except Exception as e:
            print(f"[Verifier/Verify] Failed to update run meta status: {e}")

        print(
            f"[Verifier/Verify] Verification complete: matched={matched_count}, missing={missing_count}, unexpected={unexpected_count}, runStatus={run_status}"
        )

        return {
            "runId": seed_instrument_run_id,
            "runStatus": run_status,
            "matchedEventsCount": matched_count,
            "missingEventsCount": missing_count,
            "unexpectedEventsCount": unexpected_count,
            "totalExpectedEventsCount": len(expectations),
        }

    def execute_check_run_status_process(
        self, seed_result: Dict[str, str]
    ) -> Dict[str, Any]:
        """
        Execute the check run status process for this service.

        Used by Step Functions "CheckRunStatus".

        Args:
            seed_result: The seed result dictionary
            containing the seed instrument run id

        Returns:
        {
            "status": "running|ready|timeout|unknown",
            "runId": "...",  # testInstrumentRunId value
            "observedCount": N,
            "expectedCount": N
        }
        """
        seed_instrument_run_id = seed_result.get("seedInstrumentRunId")
        meta = get_run_meta(seed_instrument_run_id)
        if not meta:
            print(
                f"[Verifier/Status] No run meta found for testInstrumentRunId={seed_instrument_run_id}"
            )
            return {"status": "unknown", "runId": seed_instrument_run_id}

        service_name = meta.get("serviceName", "all")
        expected_events_count = 0

        # Try to load expectations to get expected count
        try:
            expectations = load_service_expectations(self.service_name)
            expected_events_count = len(expectations)
            logger.info(
                f"[Verifier/Status] Loaded {expected_events_count} expectations for serviceName={service_name}"
            )
        except Exception as e:
            print(f"[Verifier/Status] Could not load expectations to get count: {e}")

        # Count observed events
        observed_events = get_observed_events(seed_instrument_run_id, "", "")
        observed_events_count = len(observed_events)

        current_status = meta.get("status", "running")
        timeout_at_str = meta.get("timeoutAt")
        now = datetime.now(timezone.utc)

        print(
            f"[Verifier/Status] Checking status for testInstrumentRunId={seed_instrument_run_id}, "
            f"currentStatus={current_status}, observedCount={observed_events_count}, "
            f"expectedCount={expected_events_count}, timeoutAt={timeout_at_str}, now={now.isoformat()}"
        )

        # Timeout check - must be done BEFORE checking if ready
        # This ensures timeout is detected even if events haven't been collected
        if timeout_at_str:
            timeout_at = parse_iso_safe(timeout_at_str)
            if timeout_at:
                # Make timeout_at timezone-aware if it's naive
                if timeout_at.tzinfo is None:
                    timeout_at = timeout_at.replace(tzinfo=timezone.utc)

                if now >= timeout_at:
                    print(
                        f"[Verifier/Status] Timeout detected: now={now}, timeoutAt={timeout_at}"
                    )
                    if current_status != "timeout":
                        try:
                            update_item_to_dynamodb(
                                key={"testId": meta["testId"], "sk": meta["sk"]},
                                UpdateExpression="SET #s = :timeout",
                                ExpressionAttributeNames={"#s": "status"},
                                ExpressionAttributeValues={":timeout": "timeout"},
                            )
                        except Exception as e:
                            print(
                                f"[Verifier/Status] Failed to set run status to timeout: {e}"
                            )
                            raise e
                    return {
                        "status": "timeout",
                        "runId": seed_instrument_run_id,
                        "observedEventsCount": observed_events_count,
                        "expectedEventsCount": expected_events_count,
                    }
            else:
                print(f"[Verifier/Status] Failed to parse timeoutAt: {timeout_at_str}")

        # If all expected events observed -> ready
        # Note: If expected_count is 0, we still check if we have observed events
        # This handles cases where expectations.json might be missing or empty
        if expected_events_count > 0:
            # Normal case: check if we've observed all expected events
            if observed_events_count >= expected_events_count:
                if current_status != "ready":
                    try:
                        update_item_to_dynamodb(
                            key={"testId": meta["testId"], "sk": meta["sk"]},
                            UpdateExpression="SET #s = :ready",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":ready": "ready"},
                        )
                    except Exception as e:
                        print(
                            f"[Verifier/Status] Failed to set run status to ready: {e}"
                        )
                        raise e
                return {
                    "status": "ready",
                    "runId": seed_instrument_run_id,
                    "observedEventsCount": observed_events_count,
                    "expectedEventsCount": expected_events_count,
                }
        else:
            # Edge case: no expectations defined, but we have observed events
            # Consider ready if we have at least some events (to avoid infinite loop)
            # This is a fallback for when expectations.json is missing or empty
            if observed_events_count > 0:
                print(
                    f"[Verifier/Status] No expectations defined (expected_count=0), but {observed_events_count} events observed. Marking as ready."
                )
                if current_status != "ready":
                    try:
                        update_item_to_dynamodb(
                            key={"testId": meta["testId"], "sk": meta["sk"]},
                            UpdateExpression="SET #s = :ready",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":ready": "ready"},
                        )
                    except Exception as e:
                        print(
                            f"[Verifier/Status] Failed to set run status to ready: {e}"
                        )
                        raise e
                return {
                    "status": "ready",
                    "runId": seed_instrument_run_id,
                    "observedEventsCount": observed_events_count,
                    "expectedEventsCount": expected_events_count,
                }

        # Otherwise still running
        return {
            "status": "running",
            "runId": seed_instrument_run_id,
            "observedEventsCount": observed_events_count,
            "expectedEventsCount": expected_events_count,
        }

    def execute_report_process(self) -> Dict[str, Any]:
        """
        Execute the report process for this service.
        """
        pass
