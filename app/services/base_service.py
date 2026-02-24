"""
Base service class for all integration test services.
All service classes should inherit from this base class.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any


class BaseService(ABC):
    """
    Abstract base class for all integration test services.
    Each service must implement the seed_sequence_run() method.
    """

    def __init__(self, service_name: str):
        """
        Initialize the service.

        Args:
            service_name: The name of the service (e.g., "sequencerunmanager")
        """
        self.service_name = service_name

    @abstractmethod
    def execute_seed_process(self) -> Dict[str, Any]:
        """
        Execute the seeding process for this service.
        """
        pass

    @abstractmethod
    def execute_verify_process(self) -> Dict[str, Any]:
        """
        Execute the verification process for this service.
        """
        pass

    @abstractmethod
    def execute_check_run_status_process(self) -> Dict[str, Any]:
        """
        Execute the check run status process for this service.
        """
        pass

    @abstractmethod
    def execute_report_process(self) -> Dict[str, Any]:
        """
        Execute the report process for this service.
        """
        pass
