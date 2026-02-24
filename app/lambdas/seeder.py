# app/service/seeder.py
"""
Seeder Lambda Function

- Create run#meta item
- Create one slot item per fixture
- Emit initial seed event to EventBridge (testMode=True, testId=runId)
"""

import json
import logging
from utils.utils import check_service_name
from utils.config import AVAILABLE_SERVICES
from services.service_registry import create_service_instance, get_registered_services

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    """
    Expected Step Functions input (test service name):
    {
        "serviceName": "workflowrunmanager"
    }

    Seeder will:
    - Generate a unique test instrument run id
    - Generate a random unique id for the test run
    - Load seed definitions from S3
    - Create a run meta item in DynamoDB
    - Publish test events to EventBridge
    - Return the test instrument run id, service name, started at, timeout at, and published count
    """
    logger.info(f"[Seeder] Event: {json.dumps(event)}")

    # retrieve payload from event
    payload = event.get("Payload") or event.get("payload")
    if not payload:
        logger.error("payload is required")
        raise ValueError("payload is required")

    service_name = payload.get("serviceName", "all")
    if not check_service_name(service_name):
        logger.error(f"Service name {service_name} is not supported")
        raise ValueError(f"Service name {service_name} is not supported")

    # Handle "all" service - run seeding for all registered services
    if service_name == "all":
        results = {}
        # Get all services from AVAILABLE_SERVICES except "all"
        services_to_process = [s for s in AVAILABLE_SERVICES if s != "all"]
        logger.info(f"[Seeder] Processing 'all' services: {services_to_process}")

        for service in services_to_process:
            try:
                # Create service instance and run seeding
                service_instance = create_service_instance(service)
                result = service_instance.execute_seed_process()
                logger.info(f"[Seeder] Result for {service}: {json.dumps(result)}")
                results[service] = result
            except Exception as e:
                logger.error(
                    f"[Seeder] Failed to seed service {service}: {e}", exc_info=True
                )
                results[service] = {"error": str(e)}

        return results

    # Handle individual service
    try:
        results = {}
        service_instance = create_service_instance(service_name)
        results[service_name] = service_instance.execute_seed_process()
        logger.info(f"[Seeder] Result: {json.dumps(results)}")
        return results
    except ValueError as e:
        logger.error(f"[Seeder] Service {service_name} not found: {e}")
        raise ValueError(
            f"Service '{service_name}' is not registered. Available services: {', '.join(get_registered_services())}"
        )
