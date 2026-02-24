# app/service/verifier.py

"""
Verifier Lambda Function

Two modes:

Status mode (called repeatedly by Step Functions):
  - Input: { "testInstrumentRunId": "...", "mode": "status" } or { "testRunId": "...", "mode": "status" } or { "runId": "...", "mode": "status" }
  - Checks run meta and returns:
      {
        "status": "running|ready|timeout|unknown",
        "runId": "...",  # testInstrumentRunId value
        "observedCount": N,
        "expectedCount": N
      }

Verify mode (called once when ready/timeout):
    - Loads expectations.json from S3
    - For each expected event:
      - Queries DynamoDB for matching events (testInstrumentRunId, detailType, source)
      - Downloads event body from S3 if found
      - Applies match rules based on expectation.__match.fields
      - Writes match info (status=matched, verifiedAt) or missing info (status=missed)
  - Checks event order
  - Checks for unexpected events (more events than expected)
  - Updates run meta status to passed/failed

Note: testInstrumentRunId format is "YYMMDD_A00001_XXXX_TESTXXXXXX" (e.g., "260122_A00001_1234_TEST123456")
"""

import json
from typing import Dict, Any
import logging
from utils.config import AVAILABLE_SERVICES
from services.service_registry import create_service_instance
from utils.utils import check_service_name

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def execute_process_for_all_services(
    event: Dict[str, Any], mode: str
) -> Dict[str, Any]:
    results = {}
    services_to_process = [s for s in AVAILABLE_SERVICES if s != "all"]
    logger.info(f"[Verifier] Processing 'all' services: {services_to_process}")
    for service in services_to_process:
        try:
            service_instance = create_service_instance(service)

            # Execute the process for the service
            if mode == "status":
                result = service_instance.execute_check_run_status_process(event)
            elif mode == "verify":
                result = service_instance.execute_verify_process(event)
            else:
                raise ValueError("mode is invalid")

            logger.info(f"[Verifier] Result for {service}: {json.dumps(result)}")
            results[service] = result
        except Exception as e:
            logger.error(
                f"[Verifier] Failed to verify service {service}: {e}", exc_info=True
            )
            results[service] = {"error": str(e)}
    logger.info(f"[Verifier] Results: {json.dumps(results)}")
    return results


def execute_process_for_service(
    service_name: str, event: Dict[str, Any], mode: str
) -> Dict[str, Any]:
    try:
        service_instance = create_service_instance(service_name)

        # Execute the process for the service
        if mode == "status":
            result = service_instance.execute_check_run_status_process(event)
        elif mode == "verify":
            result = service_instance.execute_verify_process(event)
        else:
            raise ValueError("mode is invalid")

        logger.info(f"[Verifier] Result for {service_name}: {json.dumps(result)}")
        return {service_name: result}
    except Exception as e:
        logger.error(
            f"[Verifier] Failed to verify service {service_name}: {e}", exc_info=True
        )
        return {service_name: {"error": str(e)}}


def handler(event, context):
    """
    Mode selection:
    Verifier will use the testInstrumentRunId from seedResult if it is available.

    - Status mode (called by SFN loop when status is running):
      { "testInstrumentRunId": "...", "mode": "status" }
      or
      { "testRunId": "...", "mode": "status" }
      or
      { "runId": "...", "mode": "status" }

    - Verify mode (called by SFN after status is ready or timeout):
      { "testInstrumentRunId": "...", "mode": "verify" }
      or
      { "testRunId": "...", "mode": "verify" }
      or
      { "runId": "...", "mode": "verify" }


    """
    print(f"[Verifier] Event: {json.dumps(event)}")

    mode = event.get("mode")
    service_name = event.get("serviceName")
    if not mode or not check_service_name(service_name):
        raise ValueError("mode and serviceName are required")

    if service_name == "all":
        return execute_process_for_all_services(event, mode)
    else:
        return execute_process_for_service(service_name, event, mode)
