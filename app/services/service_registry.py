"""
Service registry for mapping service names to their corresponding service classes.
This allows dynamic service discovery and easy addition of new services.
"""

from typing import Dict, Type, Optional, List
from services.base_service import BaseService
from services.sequence_srv import SequenceRunManagerService

# Registry mapping service names to their service classes
SERVICE_REGISTRY: Dict[str, Type[BaseService]] = {
    "sequencerunmanager": SequenceRunManagerService,
    # Add new services here as they are implemented
    # "workflowrunmanager": WorkflowRunManagerService,
    # "bclconvertermanager": BclConverterManagerService,
}


def get_service_class(service_name: str) -> Optional[Type[BaseService]]:
    """
    Get the service class for a given service name.

    Args:
        service_name: The name of the service (e.g., "sequencerunmanager")

    Returns:
        The service class if found, None otherwise
    """
    return SERVICE_REGISTRY.get(service_name.lower())


def register_service(service_name: str, service_class: Type[BaseService]) -> None:
    """
    Register a new service class in the registry.

    Args:
        service_name: The name of the service (e.g., "workflowrunmanager")
        service_class: The service class that inherits from BaseService
    """
    SERVICE_REGISTRY[service_name.lower()] = service_class


def get_registered_services() -> List[str]:
    """
    Get a list of all registered service names (excluding "all").

    Returns:
        List of registered service names
    """
    return list(SERVICE_REGISTRY.keys())


def create_service_instance(service_name: str) -> BaseService:
    """
    Create an instance of the service for the given service name.

    Args:
        service_name: The name of the service (e.g., "sequencerunmanager")

    Returns:
        An instance of the service class

    Raises:
        ValueError: If the service is not registered
    """
    service_class = get_service_class(service_name)
    if service_class is None:
        raise ValueError(
            f"Service '{service_name}' is not registered. "
            f"Available services: {', '.join(get_registered_services())}"
        )
    return service_class(service_name)
