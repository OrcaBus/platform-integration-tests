# Platform Integration Tests - Application Code

This directory contains the Lambda function implementations for the Platform Integration Testing service. The service validates end-to-end event flows through OrcaBus by seeding test events, collecting observed events, verifying them against expected fixtures, and generating reports.

## Overview

The service consists of four Lambda functions that work together to test event-driven workflows:

1. **Seeder** - Publishes test events to OrcaBus
2. **Collector** - Archives events from OrcaBus to DynamoDB
3. **Verifier** - Compares observed events with expected fixtures
4. **Reporter** - Generates reports and sends notifications

## Lambda Functions

### Seeder (`seeder.py`)

**Purpose**: Publishes scenario seed events to the OrcaBus staging test bus.

**Input**:
```json
{
  "scenario": "happy-path-01",
  "timeoutSeconds": 300
}
```

**Output**:
```json
{
  "statusCode": 200,
  "body": {
    "runId": "uuid",
    "scenario": "happy-path-01",
    "expectedCount": 4,
    "publishedEvents": [
      {"eventId": "uuid", "eventType": "stepA.started", "seq": 1},
      ...
    ]
  }
}
```

---

### Collector (`collector.py`)

**Purpose**: Listens to OrcaBus events and archives all events into DynamoDB with deduplication.


**What it does**:
- Filters events with `testMode=true`
- Checks if the run is still active (status = "running")
- Stores events in DynamoDB with:
  - Sort key: `event#seq000001` (if seq available) or `event#ts#2025-...Z`
  - Payload hash for idempotency checking
  - Full event payload and metadata
- Uses conditional puts to prevent duplicate `eventId`s
- Skips events for inactive runs

---

### Verifier (`verifier.py`)

**Purpose**: When a run is ready (expected count reached or timeout), verifies observed events against expected fixtures.

**What it does**:
1. **Presence Check**: Verifies all expected event types/counts are observed
2. **Order Check**: Validates either:
   - Strictly increasing `seq` values, or
   - All `causedBy` edges satisfied (DAG validation)
3. **Payload Check**: Compares event payloads against expected payloads (deep equality)
4. **Idempotency Check**: Ensures no duplicate `eventId`s
5. **Latency Check**: Verifies total duration is within configured limits

Writes the verdict to DynamoDB with detailed results and updates run status to "completed".

**Environment Variables**:
- `TABLE_NAME` - DynamoDB table name (default: `platform-it-store`)
- `MAX_LATENCY_MS` - Maximum allowed latency in milliseconds (default: `60000`)

**Verdict Structure** (stored in DynamoDB):
```json
{
  "runId": "uuid",
  "sk": "verdict#1",
  "status": "passed",
  "passed": true,
  "checks": {...},
  "failures": [],
  "metrics": {...},
  "verifiedAt": "2025-01-01T12:00:00Z"
}
```

---

### Reporter (`reporter.py`)

**Purpose**: Reads the verdict, generates HTML/JSON reports, uploads to S3 (optional), and sends Slack notifications.

**Output**:
```json
{
  "statusCode": 200,
  "body": {
    "runId": "uuid",
    "status": "passed",
    "passed": true,
    "reportUrl": "https://bucket.s3.amazonaws.com/reports/uuid_20250101_120000.html",
    "jsonReport": {...}
  }
}
```

**What it does**:
- Loads verdict, metadata, fixtures, and observed events from DynamoDB
- Generates comprehensive HTML report with:
  - Run information and metrics
  - Check results (presence, order, payload, idempotency, latency)
  - Failure details
  - Expected vs Observed event comparison table
- Generates JSON report for programmatic access
- Uploads reports to S3 (if `S3_BUCKET` configured)
- Sends Slack notification with status and report link
- Approves/rejects CodePipeline stage (if configured and tests passed)

---

## Data Flow

```
1. Step Functions invokes Seeder
   └─> Seeder publishes events to OrcaBus
   └─> Seeder stores run metadata & fixtures in DynamoDB

2. EventBridge rule triggers Collector for each OrcaBus event
   └─> Collector filters testMode=true events
   └─> Collector stores events in DynamoDB (deduplicated)

3. Step Functions waits for ready condition (count or timeout)
   └─> Step Functions invokes Verifier
       └─> Verifier loads fixtures & events from DynamoDB
       └─> Verifier performs all checks
       └─> Verifier writes verdict to DynamoDB

4. Step Functions invokes Reporter
   └─> Reporter loads verdict & data from DynamoDB
   └─> Reporter generates HTML/JSON reports
   └─> Reporter uploads to S3 (optional)
   └─> Reporter sends Slack notification
   └─> Reporter approves CodePipeline (optional)
```

## DynamoDB Schema


## Dependencies

Install Python dependencies:

```bash
pip install -r requirements.txt
```

**Required packages**:
- `boto3>=1.34.0` - AWS SDK
- `requests>=2.31.0` - HTTP client for Slack

## Testing
