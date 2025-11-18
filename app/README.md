# Platform Integration Tests - Application Code

This directory contains the Lambda function implementations for the Platform Integration Testing service. The service validates end-to-end event flows through OrcaBus by seeding test events, collecting observed events, verifying them against expected fixtures, and generating reports.

## Overview

The service consists of four Lambda functions that work together to test event-driven workflows:

1. **Seeder** - Publishes test events to OrcaBus
2. **Collector** - Archives events from OrcaBus to DynamoDB
3. **Verifier** - Compares observed events with expected fixtures
4. **Reporter** - Generates reports and sends notifications

## Components

### 1. Test Controller (Step Functions)

- Orchestrates one run from **seed → collect → verify → report**
- States (high-level):
  1. `GenerateTestId` (Pass or Lambda)
  2. `InitRunMetadata` (Lambda)
  3. `SeedScenario` (Seeder Lambda)
  4. `WaitAndCheckReady` (loop: Wait + CheckRunStatus Lambda)
  5. `Verify` (Verifier Lambda)
  6. `Report` (Reporter Lambda)
  7. `End` (success/fail output to CI)

> **Implementation**: AWS CDK (TypeScript) defines the entire state machine.

### 2. Seeder (Lambda, Python)

**Input:**

- `testId`, `scenario`

**Responsibilities:**

- Load fixtures for `scenario`
- Write fixtures to DynamoDB under `PK = RUN#<testId>`
- Set `expected_events` in run metadata
- Emit initial staging EventBridge event(s) with:
  - `testMode = true`
  - `testId = <testId>`

### 3. Collector (Lambda, Python)

**Trigger:**

- EventBridge rule: `detail.testMode = true`

**Responsibilities:**

- Filter events by `testId`
- Dedupe events (e.g., using a deterministic `eventId`)
- Save events in DynamoDB under the run partition
- Increment `received_events` in run metadata
- Optional: store full payload in S3 if large

**Resilience:**

- Configured with a DLQ (SQS) for failed invocations

### 4. Verifier (Lambda, Python)

**Input:**

- `testId`

**Responsibilities:**

- Load fixtures and observed events for the run
- Perform checks:
  - Presence, order, multiplicity, latency, schema (configurable)
- Write **verdict** item to DynamoDB
- Update run metadata `status` to `passed` or `failed`

### 5. Reporter (Lambda, Python)

**Input:**

- `testId`

**Responsibilities:**

- Load verdict and metadata from DynamoDB
- Generate HTML/JSON report and store in S3
- Update run metadata with `report_s3_key`
- Optionally send a summary notification (e.g., Slack)

## Test Run Lifecycle

Each test run is identified by a unique `testId`.

### 1. Trigger

- CodePipeline (or another CI job) calls the Step Functions **Test Controller** state machine, passing:
  - Scenario name (e.g., `stage-test`, `ad-hoc-job`)
  - Optional configuration (timeouts, strictness level)

### 2. Initialize Run

- The controller:
  - Generates a new `testId`
  - Writes an initial **run metadata** record to DynamoDB:
    - `status = "initializing"`
    - `scenario`, timestamps, etc.

### 3. Seed Scenario (Seeder Lambda)

- Loads **fixtures** for the selected scenario, e.g., from:
  - Packaged JSON files, or
  - S3 under `fixtures/<scenario>.json`
- Writes fixtures into DynamoDB under the current `testId`
- Sets `expected_events` in the run metadata (how many orchestration events we expect)
- Publishes **seed EventBridge event(s)** to the OrcaBus staging bus with:
  - `detail.testMode = true`
  - `detail.testId = <testId>`
- Orchestrated services must **propagate `testId` and `testMode`** when they publish events

### 4. Collect Events (Collector Lambda)

- An **always-on EventBridge rule** forwards events where:
  - `detail.testMode = true`
- The Collector Lambda receives these events and:
  - Filters to the current run `testId`
  - Stores each event in DynamoDB under the run partition
  - Increments `received_events` for the run (using DynamoDB atomic counters)
  - Optionally stores large payloads in S3 and keeps only pointers + key fields in DynamoDB

### 5. Wait for Readiness (Step Functions)

- The controller loops:
  - `Wait X seconds → CheckRunStatus Lambda`
- `CheckRunStatus` reads run metadata from DynamoDB and decides:
  - If `received_events >= expected_events` → mark `status = "ready"`
  - If total run time exceeds a configured timeout → mark `status = "timed_out"`
- The controller exits the loop once the run is `ready` **or** `timed_out`

### 6. Verify (Verifier Lambda)

- Verifier loads, for this `testId`:
  - Fixtures (expected events)
  - Observed events
- It performs checks such as:
  - **Presence**: expected events were emitted
  - **Order**: events occurred in the right order (if required)
  - **Multiplicity**: no unexpected duplicates
  - **Latency windows**: events arrived within configured time thresholds
  - **Schema and fields**: optional structural checks on the event payload
- Writes a **verdict record** into DynamoDB:
  - `status = "passed"` or `"failed"`
  - List of failed assertions (if any)

### 7. Report (Reporter Lambda)

- Reads verdict + run metadata + key event summaries from DynamoDB
- Generates:
  - An **HTML report** (for humans) and/or
  - A **JSON report** (for machines)
- Stores them in S3 with a key like: `reports/<date>/<testId>.html`
- Optionally posts a summary to Slack:
  - Scenario name, `testId`, verdict, link to report
- Updates run metadata to include a pointer to the report

### 8. Return Result to CI

- Step Functions ends with a payload including:
  - `testId`
  - `scenario`
  - `verdict`
  - `reportUrl`
- CodePipeline uses this output to:
  - **Allow** promotion to production if `verdict = "passed"`
  - **Block** or require manual approval if `verdict = "failed"`

## Data Model (DynamoDB)

We use a **single DynamoDB table**, partitioned by **run**, with exactly **two item types**:

1. **Run meta item** – one per test run
2. **Event slot item** – one per expected event (fixture) in the run

This keeps all data for a run in one partition and makes it easy to query and verify.

### Table Structure

**Table name**: `IntegrationTestRuns` (configurable via CDK)

**Primary key:**

- **Partition key (`pk`)**: `run#<runId>`
- **Sort key (`sk`)**: `run#meta` or `slot#...`

**Example:**

- `pk = "run#123456"`
  - `sk = "run#meta"` → run metadata
  - `sk = "slot#seq#000001"` → first expected event slot
  - `sk = "slot#seq#000002"` → second expected event slot
  - ...

All items for a given run share the same `pk`, so a single `Query` gets the entire run.

### Run Meta Item

One item per **test run**. Created by the Seeder at the start of the run.

**Keys:**

- `pk`: `run#<runId>`
- `sk`: `run#meta`

**Attributes:**

- `runId` (S) - Unique ID for this run (same as the one used as `testId` in events)
- `scenario` (S) - Human-readable scenario name (e.g., `daily-batch-orchestration`)
- `expectedSlots` (N) - How many slots/fixtures we expect for this run (number of slot items)
- `observedCount` (N) - How many slots have **at least one matching observed event**. Incremented by Collector
- `status` (S) - Run-level state: `running | ready | failed | passed | timeout`
- `startedAt` (S, ISO8601) - When the run started
- `timeoutAt` (S, ISO8601, optional) - When the run should be considered timed out
- `reportS3Key` (S, optional) - S3 key of the generated HTML/JSON report
- `ttl` (N, optional) - Unix epoch timestamp for automatic expiration (TTL)

**Example:**

```jsonc
{
  "pk": "run#123456",
  "sk": "run#meta",
  "runId": "123456",
  "scenario": "daily-batch-orchestration",
  "expectedSlots": 5,
  "observedCount": 3,
  "status": "running",
  "startedAt": "2025-01-01T10:00:00Z",
  "timeoutAt": "2025-01-01T10:02:00Z",
  "reportS3Key": null,
  "ttl": 1735689600
}
```

### Event Slot Item

One slot per expected event.

Each slot holds:

- The fixture (expected event)
- A list of observed events that matched this slot
- A per-slot verdict comparing expected vs observed events

**Keys:**

- `pk`: `run#<runId>`
- `sk`:
  - Sequential ordering: `slot#seq#000001`, `slot#seq#000002`, ...
  - DAG-style / logical IDs: `slot#id#INVOICE_CREATED`, `slot#id#BILLING_COMPLETED`, ...

**Attributes:**

- `runId` (S) - Same as in Run meta item (handy for debugging)
- `slotType` (S):
  - `seq` – this slot represents a step in a linear sequence
  - `dag` – this slot represents a node in a DAG (causal graph)
- `slotId` (S|N):
  - For `seq`: the sequence number (e.g., 1, 2, 3)
  - For `dag`: a stable ID for the slot (e.g., "INVOICE_CREATED")
- `expected` (Map) - Fixture describing the expected event for this slot

**Expected Fields** (typical, but scenario-specific fields can be added):

- `detailType` (S) - Expected EventBridge detail-type
- `source` (S, optional) - Expected EventBridge source
- `seq` (N, optional) - Position in a linear sequence (slotType = "seq")
- `causedBy` (L of S, optional) - For DAG flows: IDs of upstream slots/events this one depends on
- `payloadHash` (S, optional) - Hash of the expected payload/body
- `observedEvents` (List of Maps) - List of all observed events that were matched to this slot
- `rawS3Key` (S, optional) - S3 key of the full expected payload (if not stored inline)

**Observed Event Entry Fields:**

- `eventId` (S) - A unique ID for the observed event (e.g., EventBridge ID)
- `detailType` (S) - Observed detail-type
- `receivedAt` (S, ISO8601) - Time when Collector processed this event
- `payloadHash` (S, optional) - Hash of the observed payload/body
- `rawS3Key` (S, optional) - S3 key with the full observed payload, if needed
- `matchReason` (S, optional) - Free text or enum describing why this event was matched to this slot (e.g., "correlation_id", "seq", "manual_rule")

When no event has matched yet, `observedEvents` is an empty list (`[]`). The Collector appends to this list using `list_append` in DynamoDB.

- `verdict` (Map) - Per-slot verdict, aggregated result for this slot, typically set by the Verifier

**Verdict Fields:**

- `status` (S) - One of: `pending`, `matched`, `missing`, `mismatch`, `duplicate`, `out_of_order`
- `reasons` (L of S) - Explanation for non-matched states
- `latencyMs` (N, optional) - Latency between expected time and the first observed matching event
- `checkedAt` (S, ISO8601, optional) - When the Verifier last evaluated this slot
- `primaryObservedIndex` (N, optional) - Index in `observedEvents` considered the primary match (usually 0)

**Example:**

```json
{
  "pk": "run#123456",
  "sk": "slot#seq#000001",
  "runId": "123456",
  "slotType": "seq",
  "slotId": 1,
  "expected": {
    "detailType": "invoice.created",
    "source": "orchestrator",
    "seq": 1,
    "causedBy": [],
    "payloadHash": "abc123",
    "rawS3Key": "fixtures/123456/slot-000001.json"
  },
  "observedEvents": [
    {
      "eventId": "evt-8765",
      "detailType": "invoice.created",
      "receivedAt": "2025-01-01T10:00:02Z",
      "payloadHash": "abc123",
      "rawS3Key": "events/123456/evt-8765.json",
      "matchReason": "correlation_id"
    },
    {
      "eventId": "evt-8765-retry",
      "detailType": "invoice.created",
      "receivedAt": "2025-01-01T10:00:03Z",
      "payloadHash": "abc123",
      "rawS3Key": "events/123456/evt-8765-retry.json",
      "matchReason": "correlation_id_retry"
    }
  ],
  "verdict": {
    "status": "matched",
    "reasons": [],
    "latencyMs": 2000,
    "checkedAt": "2025-01-01T10:00:05Z",
    "primaryObservedIndex": 0
  }
}
```

### Seeder & Collector with this Data Model

**Seeder:**

- Creates:
  - 1 × Run meta item (`sk = run#meta`)
  - N × slot items (`sk = slot#...`) with:
    - `expected` filled
    - `observedEvents = []`
    - `verdict.status = "pending"`
- Emits seed events with `testMode = true` and `testId = runId`

**Collector:**

- Triggered by EventBridge for all test-mode events (`detail.testMode = true`)
- Resolves the correct slot
- Appends one entry to `observedEvents` array of that slot
- If this is the first observed event for that slot, increments `observedCount` on the Run meta item


## Dependencies

Install Python dependencies:

```bash
pip install -r deps/requirements.txt
```

**Required packages:**

- `boto3>=1.34.0` - AWS SDK for Python
- `requests>=2.31.0` - HTTP client library for Slack notifications

## Testing

For local development and testing of Lambda functions:

```bash
# Run unit tests (if available)
make test

# Install dependencies
pip install -r deps/requirements.txt
```

See the main [README.md](../README.md) for integration testing instructions and deployment procedures.
