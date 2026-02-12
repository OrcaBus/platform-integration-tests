# Available services for the integration test
# use case: check if the service name is valid and supported for the current integration test
AVAILABLE_SERVICES = [
    "all",
    "sequencerunmanager",
    "workflowrunmanager",
    "bclconvertermanager",
]


# Service abbreviations for the integration test
# use case: get the service abbreviation from the service name
SERVICE_ABBREVIATIONS = {
    "sequencerunmanager": "SRM",
    "workflowrunmanager": "WRM",
    "bclconvertermanager": "BCM",
}

# Test ID mapping for the integration test
# use case: get the test ID from the event detail type
# format: {event_detail_type: test_id_field}
TEST_ID_MAPPING = {
    "SequenceRunStateChange": "detail.instrumentRunId",
    "SequenceRunSampleSheetChange": "detail.instrumentRunId",
    "SequenceRunLibraryLinkingChange": "detail.instrumentRunId",
}
