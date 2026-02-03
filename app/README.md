# Platform Integration Tests - Application Code

This directory contains the Lambda function implementations for the Platform Integration Testing service. The service validates end-to-end event flows through OrcaBus by:

1. **Seeding** synthetic test events into OrcaBus.
2. **Collecting** all test-mode events emitted by platform services.
3. **Verifying** those observed events against expected fixtures.
4. **Reporting** the verdict and storing human-readable reports.

Everything runs as a **serverless test harness**:

- Orchestrated by **AWS Step Functions**.
- Using **EventBridge** (OrcaBus) for events.
- Using **DynamoDB** for run metadata + expectations.
- Using **S3** for seed fixtures, archived events, and reports.

---

## Components

The application code is organised around five Lambda functions:

1. **RuleController** – enables/disables the EventBridge collector rule at the start and end of test runs.
2. **Seeder** – generates testInstrumentRunId, loads seed fixtures from S3, creates run metadata in DynamoDB, and publishes test events to OrcaBus.
3. **Collector** – triggered by EventBridge when the collector rule is enabled, archives full events to S3 and writes event metadata to DynamoDB (lightweight archival only).
4. **Verifier** – evaluates whether the observed events satisfy the expectations and writes verdicts to DynamoDB.
5. **Reporter** – generates an HTML report for a test run and stores it in S3.

There is also an **orchestrating Step Functions state machine** defined in CDK that wires these Lambdas together.

---

## 1. Test Controller (Step Functions)

The Step Functions state machine orchestrates one full test run:

```text
EnableCollectorRule (RuleController Lambda)
  └─> SeedScenario (Seeder)
       └─> Wait / Status Loop:
            ├─> Wait (1 minute)
            ├─> CheckRunStatus (Verifier in "status" mode)
            └─> Choice:
                 ├─> If ready/timeout: VerifyRun (Verifier in "verify" mode)
                 │    └─> ReportRun (Reporter)
                 │         └─> DisableCollectorRule
                 │              └─> Done
                 └─> If running: loop back to Wait
```

**Execution input (simplified):**

```jsonc
{
  "serviceName": "workflowrunmanager"  // or "all" or omitted
}
```

- `serviceName = "all"` or omitted → seed **all** services scenario.
- `serviceName = "<service>"` → seed only the fixtures for that service.

### State machine behaviour

1. **EnableCollectorRule**
   - A small Lambda that calls `events:EnableRule` for the integration-test collector rule.
   - The rule routes all `testMode` events from OrcaBus into the **Collector** Lambda.

2. **SeedScenario (Seeder Lambda)**
   - Generates a unique `testInstrumentRunId` (format: `YYMMDD_A00001_XXXX_TESTXXXXXX`, e.g., `260122_A00001_1234_TEST123456`).
   - Resolves `serviceName` (normalising and falling back to `all` if specific seeds are missing).
   - Loads seed fixtures from S3 (`seeds.json` only; expectations are loaded by Verifier).
   - Writes:
     - One **run meta** item (`testId=run#{testInstrumentRunId}`, `sk=run#meta`) in DynamoDB.
   - Publishes test events to the EventBridge bus sequentially with a 1-second delay between events (simulating real service behaviour).
   - Applies dynamic field replacement using `__replace` configuration:
     - `randomUniqueIdField`: Generates random unique IDs for specified fields
     - `testInstrumentRunIdField`: Replaces with the generated testInstrumentRunId
     - `timeStampField`: Replaces with current timestamp
   - Returns:

     ```json
     {
       "testInstrumentRunId": "260122_A00001_1234_TEST123456",
       "serviceName": "workflowrunmanager",
       "startedAt": "...",
       "timeoutAt": "...",
       "publishedCount": 5
     }
     ```

3. **Wait / Status Loop (Verifier in "status" mode)**
   - The state machine waits 1 minute, then calls Verifier with:

     ```json
     { "testInstrumentRunId": "260122_A00001_1234_TEST123456", "mode": "status" }
     ```
     Note: Also accepts `testRunId` or `runId` for backward compatibility.

   - Verifier loads expectations from S3 to count expected events, counts observed events from DynamoDB, and returns:

     ```json
     {
       "status": "running|ready|timeout|unknown",
       "runId": "260122_A00001_1234_TEST123456",
       "observedCount": 3,
       "expectedCount": 5
     }
     ```

   - Loop continues while `status = "running"`.
   - Exits the loop when `status = "ready"` (all expected events observed) or `status = "timeout"`.

4. **VerifyRun (Verifier in "verify" mode)**
   - Called once the run is `ready` or `timeout`:

     ```json
     { "testInstrumentRunId": "260122_A00001_1234_TEST123456", "mode": "verify" }
     ```
     Note: Also accepts `testRunId` or `runId` for backward compatibility.

   - Verifier loads `expectations.json` from S3, queries DynamoDB for observed events, matches them using `__match.fields` rules, and writes results:

     ```json
     {
       "runId": "260122_A00001_1234_TEST123456",
       "runStatus": "passed|failed",
       "matchedCount": 4,
       "missingCount": 1,
       "unexpectedCount": 0,
       "totalExpected": 5
     }
     ```

5. **ReportRun (Reporter)**
   - Reporter receives:

     ```json
     {
       "testInstrumentRunId": "260122_A00001_1234_TEST123456",
       "serviceName": "workflowrunmanager",
       "verifyResult": { ... }
     }
     ```
     Note: Also accepts `testRunId` or `runId` for backward compatibility.

   - Generates an HTML report and stores it in S3 at:

     ```text
     reports/testruns/{serviceName}/{YYYY}/{MM}/{DD}/{timestamp}-{testInstrumentRunId}.html
     ```

   - Returns the S3 location (`bucket`, `key`, `url`) to the state machine.

6. **DisableCollectorRule**
   - Disables the EventBridge rule again so the collector only runs during active test windows.

7. **Done**
   - The state machine returns a final payload including `testInstrumentRunId`, `serviceName`, and the verification + report information.

---

## 2. RuleController (Lambda, Python)

**Input (from Step Functions):**

```jsonc
{
  "action": "enable" | "disable",
  "serviceName": "workflowrunmanager"  // optional, passed through
}
```

**Responsibilities:**

- Enables or disables the EventBridge collector rule that routes test-mode events to the Collector Lambda.
- Called at the start of each test run (`action: "enable"`) and at the end (`action: "disable"`).
- Ensures the Collector only processes events during active test windows.

**Output:**

```jsonc
{
  "ruleName": "<RULE_NAME>",
  "eventBusName": "<EVENT_BUS_NAME>",
  "action": "enable" | "disable",
  "status": "ok",
  "serviceName": "workflowrunmanager"
}
```

---

## 3. Seeder (Lambda, Python)

**Input (from Step Functions):**

```jsonc
{
  "serviceName": "workflowrunmanager"  // optional, default "all"
}
```

**Responsibilities:**

- Generate a unique `testInstrumentRunId` (format: `YYMMDD_A00001_XXXX_TESTXXXXXX`, e.g., `260122_A00001_1234_TEST123456`).
- Resolve `serviceName` (if service-specific fixtures don't exist, fall back to `"all"`).
- Load seed events from S3 (`seed/services/{serviceName}/seeds.json`).
- Create a **run meta item** in DynamoDB (`testId=run#{testInstrumentRunId}`, `sk=run#meta`).
- Apply dynamic field replacement using `__replace` configuration in seed events:
  - `randomUniqueIdField`: Generates random unique IDs (with optional prefix/suffix formatting)
  - `testInstrumentRunIdField`: Replaces specified fields with the generated testInstrumentRunId
  - `timeStampField`: Replaces specified fields with current ISO timestamp
- Publish test events to EventBridge sequentially with a 1-second delay between events (simulating real service behaviour).

The Collector extracts `instrumentRunId` from events using a mapping based on `detail-type` to identify which events belong to a specific test run.

---

## 4. Collector (Lambda, Python)

**Trigger:**

- EventBridge rule on the OrcaBus staging bus (enabled/disabled by the RuleController Lambda).
- The rule filters on `detail.testMode = true` or specific event patterns.

**Responsibilities:**

For each EventBridge event:

```jsonc
{
  "id": "...",
  "source": "...",
  "detail-type": "SequenceRunStateChange",
  "detail": {
    "instrumentRunId": "260122_A00001_1234_TEST123456",
    "...": "..."
  }
}
```

The collector performs lightweight archival only (no matching logic):

1. **Filters test events**
   - Ignores seed events from sources: `orcabus.integrationtests`, `orcabus.integrationtests.seed`.
   - Extracts `instrumentRunId` from event using a mapping based on `detail-type` (configured in `helper.py`).
   - Ensures `run#meta` exists in DynamoDB for this `testInstrumentRunId` (`testId=run#{testInstrumentRunId}`, `sk=run#meta`).
   - If not found, the event is ignored (e.g. stray or late event).

2. **Archives full event to S3**
   - Stores the entire EventBridge event to:

     ```text
     events/testruns/{testInstrumentRunId}/year={YYYY}/month={MM}/day={DD}/{detailType}_{timestamp}.json
     ```

3. **Writes event metadata to DynamoDB**
   - Creates an event record with:
     - `testId`: `run#{testInstrumentRunId}`
     - `sk`: `event#{timestamp}`
     - `detailType`: from the event
     - `source`: from the event
     - `payloadHash`: SHA256 hash of the detail payload
     - `rawS3Key`: S3 key where the full event is stored
     - `receivedAt`: ISO timestamp

   This allows Verifier to query events by `testId`, `detailType`, and `source`, then download full event bodies from S3 for matching.

---

## 5. Verifier (Lambda, Python)

Verifier runs in two modes: **status** and **verify**.

### Status Mode

**Input:**

```jsonc
{ "testInstrumentRunId": "260122_A00001_1234_TEST123456", "mode": "status" }
```
Note: Also accepts `testRunId` or `runId` for backward compatibility.

**Responsibilities:**

- Load the **run meta item**:

  ```jsonc
  {
    "testId": "run#260122_A00001_1234_TEST123456",
    "sk": "run#meta",
    "serviceName": "workflowrunmanager",
    "observedCount": 3,
    "status": "running",
    "startedAt": "...",
    "timeoutAt": "..."
  }
  ```

- Decide:

  - If now >= `timeoutAt` → mark status `timeout` and return `"timeout"`.
  - Else if `observedCount >= expectedSlots` → mark status `ready`.
  - Else → status stays `running`.

- Output:

  ```jsonc
  {
    "status": "running|ready|timeout|unknown",
    "runId": "it-1234",
    "observedCount": 3,
    "expectedSlots": 5
  }
  ```

The Step Functions loop uses this to decide when to move on to full verification.

### Verify Mode

**Input:**

```jsonc
{ "testInstrumentRunId": "260122_A00001_1234_TEST123456", "mode": "verify" }
```
Note: Also accepts `testRunId` or `runId` for backward compatibility.

**Responsibilities:**

1. **Load expectations from S3**
   - Loads `expectations.json` from `seed/services/{serviceName}/expectations.json`.

2. **For each expected event (in order):**
   - Query DynamoDB for observed events matching `testId=run#{testInstrumentRunId}`, `detailType`, and `source`.
   - Download full event body from S3 using `rawS3Key`.
   - Apply match rules based on `expectation.__match.fields` (dot-notation paths like `"detail.instrumentRunId"`).
   - If matched:
     - Write match info to the event item: `status=matched`, `verifierAt`, `expectedOrder`, `expectedEvent`.
   - If not matched:
     - Write missing event item to DynamoDB: `testId=run#{testInstrumentRunId}`, `sk=expectation#{order}-missing`, `detailType`, `source`, `expectedEvent`, `status=missed`, `verifierAt`, `expectedOrder`.

3. **Check for unexpected events**
   - After all expected events are checked, query DynamoDB for any observed events that weren't matched.
   - Mark them as `status=unexpected`.

4. **Determine overall run status**
   - If any missing or unexpected events → `runStatus = "failed"`.
   - If run meta status is `timeout` → `runStatus = "failed"`.
   - Otherwise → `runStatus = "passed"`.

5. **Update run meta and return:**

  ```jsonc
  {
    "runId": "260122_A00001_1234_TEST123456",
    "runStatus": "passed|failed",
    "matchedCount": 5,
    "missingCount": 0,
    "unexpectedCount": 0,
    "totalExpected": 5
  }
  ```

---

## 6. Reporter (Lambda, Python)

**Input (from Step Functions):**

```jsonc
{
  "testInstrumentRunId": "260122_A00001_1234_TEST123456",
  "serviceName": "workflowrunmanager",
  "verifyResult": {
    "runId": "260122_A00001_1234_TEST123456",
    "runStatus": "passed",
    "matchedCount": 5
  }
}
```
Note: Also accepts `testRunId` or `runId` for backward compatibility.

**Responsibilities:**

- Load run meta from DynamoDB (`testId=run#{testInstrumentRunId}`, `sk=run#meta`) to get additional details (`startedAt`, `verifiedAt`, etc.).
- Query DynamoDB for:
  - Matched events (`testId=run#{testInstrumentRunId}`, `sk` begins with `event#`, `status=matched`)
  - Missing events (`testId=run#{testInstrumentRunId}`, `sk` begins with `expectation#`, `status=missed`)
  - Unexpected events (`testId=run#{testInstrumentRunId}`, `sk` begins with `event#`, `status=unexpected`)
- Generate an HTML report with detailed tables showing matched, missing, and unexpected events.
- Store the report in S3 with a **timestamp-first filename**:

  ```text
  reports/testruns/{serviceName}/{YYYY}/{MM}/{DD}/{timestamp}-{testInstrumentRunId}.html
  ```

  Example:

  ```text
  reports/testruns/workflowrunmanager/2025/11/21/
    2025-11-21T10-15-32Z-260122_A00001_1234_TEST123456.html
  ```

- Update `run#meta` with `reportS3Key`.
- Return the S3 location to the state machine:

  ```jsonc
  {
    "bucket": "<S3_BUCKET>",
    "key": "reports/testruns/workflowrunmanager/2025/11/21/2025-11-21T10-15-32Z-260122_A00001_1234_TEST123456.html",
    "url": "s3://<S3_BUCKET>/reports/testruns/workflowrunmanager/2025/11/21/2025-11-21T10-15-32Z-260122_A00001_1234_TEST123456.html"
  }
  ```

---

## 6. S3 Layout

All fixtures, archived events, and reports live in a single S3 bucket (one per environment/account). The layout is:

```text
s3://<bucket>/
  seed/
    services/
      all/
        seeds.json              # JSON array of seed events for the "all" scenario
        expectations.json       # JSON array of expectations for "all"
      workflowrunmanager/
        seeds.json              # JSON array of seed events for this service
        expectations.json       # JSON array of expectations for this service
      <other-service>/
        seeds.json
        expectations.json

  events/
    testruns/
      260122_A00001_1234_TEST123456/
        year=2025/
          month=11/
            day=21/
              SequenceRunStateChange_20251121T100005.123.json
              SequenceRunStateChange_20251121T100010.456.json
      260122_A00001_5678_TEST789012/
        ...

  reports/
    templates/
      base.html                 # Main HTML template (used by Reporter)
      per_service.html          # Optional service-specific template(s)

    testruns/
      all/
        2025/11/21/2025-11-21T09-30-00Z-260122_A00001_0001_TEST000001.html
      workflowrunmanager/
        2025/11/21/2025-11-21T10-15-32Z-260122_A00001_1234_TEST123456.html
      <other-service>/
        ...
```

**Seed files format (example):**

```jsonc
// seed/services/workflowrunmanager/seeds.json
[
  {
    "version": "0",
    "id": "r.it001",
    "detail-type": "Event from aws:sqs",
    "source": "Pipe IcaEventPipeConstru-IntegrationTest",
    "account": "000000000000",
    "time": "2025-11-25T02:00:00Z",
    "region": "ap-southeast-2",
    "resources": [],
    "detail": {
      "ica-event": {
        "gdsFolderPath": "",
        "gdsVolumeName": "bssh.testvolume.it001",
        "reagentBarcode": "FAKE123456-RGTEST",
        "instrumentRunId": "251125_A01052_0001_IT001",
        "status": "New"
      }
    },
    "__replace": {
      "randomUniqueIdField": [
        {
          "name": "detail.icaEvent.id",
          "format": {
            "prefix": "r."
          }
        }
      ],
      "testInstrumentRunIdField": [
        "detail.icaEvent.instrumentRunId",
        "detail.icaEvent.name"
      ],
      "timeStampField": [
        "time",
        "detail.icaEvent.dateModified"
      ]
    }
  }
]
```

```jsonc
// seed/services/workflowrunmanager/expectations.json
[
  {
    "detail-type": "SequenceRunStateChange",
    "source": "orcabus.sequencerunmanager",
    "detail": {
      "id": "seq.IT001",
      "instrumentRunId": "251125_A01052_0001_IT001",
      "runVolumeName": "bssh.testvolume.it001",
      "status": "STARTED"
    },
    "__match": {
      "fields": [
        "detail-type",
        "source",
        "detail.instrumentRunId",
        "detail.runVolumeName",
        "detail.status"
      ]
    }
  }
]
```

---

## 7. DynamoDB Data Model

We use a **single DynamoDB table** for everything (configured via `TABLE_NAME` environment variable). All data for a test run is kept in a **single partition**, keyed by `testId=run#{testInstrumentRunId}`.

### Primary Key

- **Partition key (`testId`)**: `run#{testInstrumentRunId}`
- **Sort key (`sk`)**:
  - `run#meta` for the run metadata item.
  - `event#{timestamp}` for observed events (from Collector).
  - `expectation#{order}-missing` for missing expected events (from Verifier).

### Example Partition for One Run

```text
testId = "run#260122_A00001_1234_TEST123456"
│
├─ sk = "run#meta"
│    {
│      "testId": "run#260122_A00001_1234_TEST123456",
│      "sk": "run#meta",
│      "serviceName": "workflowrunmanager",
│      "status": "passed",
│      "startedAt": "2025-11-21T10:00:00Z",
│      "timeoutAt": "2025-11-21T10:15:00Z",
│      "verifiedAt": "2025-11-21T10:10:00Z",
│      "reportS3Key": "reports/testruns/workflowrunmanager/2025/11/21/2025-11-21T10-15-32Z-260122_A00001_1234_TEST123456.html"
│    }
│
├─ sk = "event#20251121T100005.123"
│    {
│      "testId": "run#260122_A00001_1234_TEST123456",
│      "sk": "event#20251121T100005.123",
│      "detailType": "SequenceRunStateChange",
│      "source": "orcabus.sequencerunmanager",
│      "payloadHash": "abc123...",
│      "rawS3Key": "events/testruns/260122_A00001_1234_TEST123456/year=2025/month=11/day=21/SequenceRunStateChange_20251121T100005.123.json",
│      "receivedAt": "2025-11-21T10:00:05Z",
│      "status": "matched",  // set by Verifier
│      "verifierAt": "2025-11-21T10:10:00Z",
│      "expectedOrder": 0,
│      "expectedEvent": { ... }  // full expectation from expectations.json
│    }
│
└─ sk = "expectation#001-missing"
     {
       "testId": "run#260122_A00001_1234_TEST123456",
       "sk": "expectation#001-missing",
       "detailType": "SequenceRunStateChange",
       "source": "orcabus.sequencerunmanager",
       "expectedEvent": { ... },  // full expectation from expectations.json
       "status": "missed",
       "verifierAt": "2025-11-21T10:10:00Z",
       "expectedOrder": 1
     }
```

### Run Meta Item (Summary)

| Attribute       | Type | Description                                                  |
|----------------|------|--------------------------------------------------------------|
| `testId`       | S    | `run#{testInstrumentRunId}` (e.g., `run#260122_A00001_1234_TEST123456`) |
| `sk`           | S    | `run#meta`                                                   |
| `serviceName`  | S    | Effective service scenario used (`all`, `workflowrunmanager`, etc.) |
| `observedCount`| N   | Number of observed events (updated by Collector)            |
| `verifiedAt`   | S    | ISO timestamp when Verifier completed verification          |
| `status`       | S    | `running`, `ready`, `timeout`, `passed`, or `failed`         |
| `startedAt`    | S    | ISO timestamp when Seeder started the run                    |
| `timeoutAt`    | S    | ISO timestamp after which the run is considered timed-out    |
| `reportS3Key`  | S    | (optional) S3 key of the HTML report                         |
| `ttl`          | N    | (optional) epoch seconds for automatic expiration            |

### Event Item (from Collector)

| Attribute          | Type | Description                                                 |
|--------------------|------|-------------------------------------------------------------|
| `testId`           | S    | `run#{testInstrumentRunId}`                                 |
| `sk`               | S    | `event#{timestamp}` (e.g., `event#20251121T100005.123`)   |
| `detailType`       | S    | Event detail-type                                           |
| `source`           | S    | Event source                                                |
| `payloadHash`      | S    | SHA256 hash of the detail payload                           |
| `rawS3Key`         | S    | S3 key where full event is stored (year=YYYY/month=MM/day=DD format) |
| `receivedAt`       | S    | ISO timestamp when event was received                       |
| `status`           | S    | Set by Verifier: `matched`, `unexpected`, or null          |
| `verifierAt`       | S    | ISO timestamp when Verifier processed this event            |
| `expectedOrder`    | N    | Order index of the matched expectation (if matched)         |
| `expectedEvent`    | M    | Full expectation object (if matched)                        |

### Missing Event Item (from Verifier)

| Attribute          | Type | Description                                                 |
|--------------------|------|-------------------------------------------------------------|
| `testId`           | S    | `run#{testInstrumentRunId}`                                 |
| `sk`               | S    | `expectation#{order}-missing` (e.g., `expectation#001-missing`) |
| `detailType`       | S    | Expected detail-type                                        |
| `source`           | S    | Expected source                                             |
| `expectedEvent`    | M    | Full expectation object from expectations.json              |
| `status`           | S    | `missed`                                                    |
| `verifierAt`       | S    | ISO timestamp when Verifier marked this as missing          |
| `expectedOrder`    | N    | Order index in expectations.json                            |

---

## 8. Dependencies

Install Python Lambda dependencies from `deps/requirements.txt`:

```bash
pip install -r deps/requirements.txt
```

Typical packages:

- `boto3>=1.34.0` – AWS SDK for Python
- `requests>=2.31.0` – HTTP client library (e.g. for Slack notifications)

---

## 9. Local Testing

For local development and testing of Lambda functions:

```bash
# Run unit tests (if present)
make test

# Install dependencies
pip install -r deps/requirements.txt
```

For integration testing, see the main repository [README.md](../README.md) for design diagram, deployment and end-to-end execution instructions.

---

## 10. Testing

This directory contains unit tests for all Lambda functions in the integration testing platform.

### Test Structure

- `test/base_test_case.py`: Base test case class with common setup/teardown for mocking AWS services
- `test/test_collector.py`: Tests for the collector lambda function
- `test/test_seeder.py`: Tests for the seeder lambda function
- `test/test_verifier.py`: Tests for the verifier lambda function
- `test/test_reporter.py`: Tests for the reporter lambda function
- `test/test_rule_controller.py`: Tests for the rule_controller lambda function

### Running Tests

To run all tests:

```bash
python -m pytest app/test/ -v
```

Or using the Makefile:

```bash
make test
```

To run a specific test file:

```bash
python -m pytest app/test/test_collector.py -v
```

To run a specific test:

```bash
python -m pytest app/test/test_collector.py::CollectorTests::test_handler_stores_event_successfully -v
```

### Test Dependencies

Install test dependencies:

```bash
pip install -r deps/requirements-test.txt
```

The tests use:
- `unittest.TestCase` for test framework
- `unittest.mock` for mocking Python objects
- `pytest` for test runner and assertions
- `boto3` with LocalStack endpoints for AWS service mocking

### Environment Setup

Tests automatically set up the following environment variables:
- `AWS_DEFAULT_REGION`: ap-southeast-2
- `TABLE_NAME`: test-table
- `S3_BUCKET`: test-bucket
- `EVENT_BUS_NAME`: test-event-bus
- `RULE_NAME`: test-rule

### Test Coverage

Each lambda function has tests covering:
- Happy path scenarios
- Error handling
- Edge cases
- Input validation
- Integration with AWS services (mocked)
