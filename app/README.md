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

The application code is organised around four Lambda functions:

1. **Seeder** – loads seed fixtures from S3, writes expectations to DynamoDB, and publishes test events to OrcaBus.
2. **Collector** – triggered by OrcaBus test events, archives full events to S3 and attaches them to the matching expectations in DynamoDB.
3. **Verifier** – evaluates whether the observed events satisfy the expectations and writes verdicts to DynamoDB.
4. **Reporter** – generates an HTML report for a test run and stores it in S3.

There is also an **orchestrating Step Functions state machine** defined in CDK that wires these Lambdas together.

---

## 1. Test Controller (Step Functions)

The Step Functions state machine orchestrates one full test run:

```text
EnableCollectorRule (RuleController Lambda)
  └─> SeedScenario (Seeder)
       └─> Wait / Status Loop (Verifier in "status" mode)
            └─> VerifyRun (Verifier in "verify" mode")
                 └─> ReportRun (Reporter)
                      └─> DisableCollectorRule
                           └─> Done
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
   - Calculates a new `testRunId` (e.g. `it-<uuid>`).
   - Resolves `serviceName` (normalising and falling back to `all` if specific seeds are missing).
   - Loads seed fixtures from S3 (events + expectations).
   - Writes:
     - One **run meta** item (`run#meta`) in DynamoDB.
     - One **expectation item** per expected event.
   - Publishes test events to the EventBridge bus (sequentially with a delay between events).
   - Returns:

     ```json
     {
       "testRunId": "it-1234",
       "serviceName": "workflowrunmanager",
       "expectedSlots": 5,
       "seededEventsCount": 5,
       "startedAt": "...",
       "timeoutAt": "..."
     }
     ```

3. **Wait / Status Loop (Verifier in "status" mode)**
   - The state machine waits (e.g. 5 seconds), then calls Verifier with:

     ```json
     { "testRunId": "it-1234", "mode": "status" }
     ```

   - Verifier reads the **run meta** item and returns:

     ```json
     {
       "status": "running|ready|timeout|unknown",
       "runId": "it-1234",
       "observedCount": 3,
       "expectedSlots": 5
     }
     ```

   - Loop continues while `status = "running"`.
   - Exits the loop when `status = "ready"` (all expectations have at least one observed event) or `status = "timeout"`.

4. **VerifyRun (Verifier in "verify" mode)**
   - Called once the run is `ready` or `timeout`:

     ```json
     { "testRunId": "it-1234", "mode": "verify" }
     ```

   - Verifier loads all **expectation items** and their `observedEvents`, computes verdicts per expectation, and overall run status:

     ```json
     {
       "runId": "it-1234",
       "runStatus": "passed|failed",
       "slotStatusCounts": {
         "matched": 4,
         "missing": 1
       }
     }
     ```

5. **ReportRun (Reporter)**
   - Reporter receives:

     ```json
     {
       "testRunId": "it-1234",
       "serviceName": "workflowrunmanager",
       "verifyResult": { ... }
     }
     ```

   - Generates an HTML report and stores it in S3 at:

     ```text
     reports/testruns/{serviceName}/{YYYY}/{MM}/{DD}/{timestamp}-{testRunId}.html
     ```

   - Returns the S3 location (`bucket`, `key`, `url`) to the state machine.

6. **DisableCollectorRule**
   - Disables the EventBridge rule again so the collector only runs during active test windows.

7. **Done**
   - The state machine returns a final payload including `testRunId`, `serviceName`, and the verification + report information.

---

## 2. Seeder (Lambda, Python)

**Input (from Step Functions):**

```jsonc
{
  "serviceName": "workflowrunmanager"  // optional, default "all"
}
```

**Responsibilities:**

- Generate a unique `testRunId` (e.g. `it-<uuid>`).
- Resolve `serviceName` (if service-specific fixtures don’t exist, fall back to `"all"`).
- Load seed fixtures from S3 (see [S3 Layout](#s3-layout)).
- Create a **run meta item** in DynamoDB for `testRunId`.
- Create one **expectation item** per expected event for this run.
- Publish test events to EventBridge sequentially with a delay (simulating real service behaviour).

Each event published by Seeder includes in its `detail`:

```jsonc
{
  "testRunId": "<testRunId>",
  "serviceName": "<effectiveServiceName>",
  "testMode": true,
  // ... scenario-specific fields
}
```

This allows Collector to filter events belonging to a specific test run.

---

## 3. Collector (Lambda, Python)

**Trigger:**

- EventBridge rule on the OrcaBus staging bus (enabled/disabled by the controller).
- The rule typically filters on `detail.testMode = true`.

**Responsibilities:**

For each EventBridge event:

```jsonc
{
  "id": "...",
  "source": "...",
  "detail-type": "SomeDetailType",
  "detail": {
    "testMode": true,
    "testRunId": "it-1234",
    "serviceName": "workflowrunmanager",
    "...": "..."
  }
}
```

The collector:

1. **Filters test events**
   - Ignores events without `detail.testRunId`.
   - Ignores events where `detail.testMode != true`.

2. **Ensures run exists**
   - Loads `run#meta` from DynamoDB for this `testRunId`.
   - If not found, the event is ignored (e.g. stray or late event).

3. **Archives full event to S3**
   - Stores the entire EventBridge event to:

     ```text
     events/testruns/{testRunId}/{YYYY}/{MM}/{DD}/{timestamp}-{eventId}.json
     ```

4. **Attaches observed event to an expectation**
   - Queries all expectation items for this run:
     - `pk = run#{testRunId}`
     - `sk begins_with "expectation#"`
   - Naively picks an expectation whose `expected.detailType` matches the event’s `detail-type`:
     - Prefer an expectation with no `observedEvents` yet.
     - Otherwise, pick the first matching one.
   - Appends a new entry to `observedEvents` on that expectation, including:
     - `eventId`
     - `detailType`
     - `receivedAt`
     - `payloadHash`
     - `rawS3Key` (where the full event is stored in S3)
     - `matchReason` (e.g. `"detailType"`)

5. **Increments run-level `observedCount`**
   - If this is the **first observed event** for an expectation, increments `observedCount` on the `run#meta` item.
   - This allows Verifier (status mode) to treat the run as **ready** once `observedCount >= expectedSlots`.

---

## 4. Verifier (Lambda, Python)

Verifier runs in two modes: **status** and **verify**.

### Status Mode

**Input:**

```jsonc
{ "testRunId": "it-1234", "mode": "status" }
```

**Responsibilities:**

- Load the **run meta item**:

  ```jsonc
  {
    "pk": "run#it-1234",
    "sk": "run#meta",
    "runId": "it-1234",
    "serviceName": "workflowrunmanager",
    "expectedSlots": 5,
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
{ "testRunId": "it-1234", "mode": "verify" }
```

**Responsibilities:**

- Load `run#meta`.
- Load all **expectation items** for this run.

- For each expectation:
  - No `observedEvents` → `status = "missing"`.
  - Exactly 1 `observedEvents`:
    - If both expected and observed have `payloadHash` and they differ → `status = "mismatch"`.
    - Else → `status = "matched"`.
  - More than 1 `observedEvents` → `status = "duplicate"`.
  - Optional: compute `latencyMs` between `run.meta.startedAt` and the first `receivedAt`.

- Writes **per-expectation verdict**:

  ```jsonc
  "verdict": {
    "status": "matched|missing|mismatch|duplicate|pending",
    "reasons": ["..."],
    "latencyMs": 1234,
    "checkedAt": "2025-11-21T10:10:00Z",
    "primaryObservedIndex": 0
  }
  ```

- Aggregates an **overall run status**:
  - If any expectation is `missing`, `mismatch`, or `duplicate` → `runStatus = "failed"`.
  - If run meta status is `timeout` → `runStatus = "failed"` (or keep `"timeout"` if you prefer).
  - Otherwise → `runStatus = "passed"`.

- Writes run-level status onto `run#meta` and returns:

  ```jsonc
  {
    "runId": "it-1234",
    "runStatus": "passed|failed",
    "slotStatusCounts": {
      "matched": 5,
      "missing": 0,
      "mismatch": 0,
      "duplicate": 0
    }
  }
  ```

---

## 5. Reporter (Lambda, Python)

**Input (from Step Functions):**

```jsonc
{
  "testRunId": "it-1234",
  "serviceName": "workflowrunmanager",
  "verifyResult": {
    "runId": "it-1234",
    "runStatus": "passed",
    "slotStatusCounts": { "matched": 5 }
  }
}
```

**Responsibilities:**

- Generate an HTML report using a simple template (or Jinja2, via the deps layer).
- Store the report in S3 with a **timestamp-first filename**:

  ```text
  reports/testruns/{serviceName}/{YYYY}/{MM}/{DD}/{timestamp}-{testRunId}.html
  ```

  Example:

  ```text
  reports/testruns/workflowrunmanager/2025/11/21/
    2025-11-21T10-15-32Z-it-1234.html
  ```

- Optionally update `run#meta` with `reportS3Key` (if desired).
- Return the S3 location to the state machine:

  ```jsonc
  {
    "bucket": "<S3_BUCKET>",
    "key": "reports/testruns/workflowrunmanager/2025/11/21/2025-11-21T10-15-32Z-it-1234.html",
    "url": "s3://<S3_BUCKET>/reports/testruns/workflowrunmanager/2025/11/21/2025-11-21T10-15-32Z-it-1234.html"
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
        events.json             # JSON array of seed events for the "all" scenario
        expectations.json       # JSON array of expectations for "all"
      workflowrunmanager/
        events.json             # JSON array of seed events for this service
        expectations.json       # JSON array of expectations for this service
      <other-service>/
        events.json
        expectations.json

  events/
    testruns/
      it-1234/
        2025/
          11/
            21/
              2025-11-21T10-00-00Z-<event-id-1>.json
              2025-11-21T10-00-10Z-<event-id-2>.json
      it-5678/
        ...

  reports/
    templates/
      base.html                 # Main HTML template (used by Reporter)
      per_service.html          # Optional service-specific template(s)

    testruns/
      all/
        2025/11/21/2025-11-21T09-30-00Z-it-0001.html
      workflowrunmanager/
        2025/11/21/2025-11-21T10-15-32Z-it-1234.html
      <other-service>/
        ...
```

**Seed files format (example):**

```jsonc
// seed/services/workflowrunmanager/events.json
[
  {
    "Source": "orca.integrationtest",
    "DetailType": "WorkflowRunCreated",
    "Detail": {
      "workflowId": "wf-123",
      "status": "created"
    }
  },
  {
    "Source": "orca.integrationtest",
    "DetailType": "WorkflowRunUpdated",
    "Detail": {
      "workflowId": "wf-123",
      "status": "running"
    }
  }
]
```

```jsonc
// seed/services/workflowrunmanager/expectations.json
[
  {
    "id": "001",
    "detailType": "WorkflowRunCreated",
    "payloadHash": "abc123",      // optional, used by verifier if present
    "...": "..."
  },
  {
    "id": "002",
    "detailType": "WorkflowRunUpdated"
  }
]
```

---

## 7. DynamoDB Data Model

We use a **single DynamoDB table** for everything (configured via `TABLE_NAME` environment variable). All data for a test run is kept in a **single partition**, keyed by `run#<testRunId>`.

### Primary Key

- **Partition key (`pk`)**: `run#<testRunId>`
- **Sort key (`sk`)**:
  - `run#meta` for the run metadata item.
  - `expectation#<id>` for each expectation.

### Example Partition for One Run

```text
pk = "run#it-1234"
│
├─ sk = "run#meta"
│    {
│      "pk": "run#it-1234",
│      "sk": "run#meta",
│      "runId": "it-1234",
│      "serviceName": "workflowrunmanager",
│      "expectedSlots": 2,
│      "observedCount": 2,
│      "status": "passed",
│      "startedAt": "2025-11-21T10:00:00Z",
│      "timeoutAt": "2025-11-21T10:15:00Z",
│      "reportS3Key": "reports/testruns/workflowrunmanager/2025/11/21/2025-11-21T10-15-32Z-it-1234.html"
│    }
│
├─ sk = "expectation#001"
│    {
│      "pk": "run#it-1234",
│      "sk": "expectation#001",
│      "testRunId": "it-1234",
│      "serviceName": "workflowrunmanager",
│      "expected": {
│        "detailType": "WorkflowRunCreated",
│        "payloadHash": "abc123"
│      },
│      "observedEvents": [
│        {
│          "eventId": "evt-1",
│          "detailType": "WorkflowRunCreated",
│          "receivedAt": "2025-11-21T10:00:05Z",
│          "payloadHash": "abc123",
│          "rawS3Key": "events/testruns/it-1234/2025/11/21/2025-11-21T10-00-05Z-evt-1.json",
│          "matchReason": "detailType"
│        }
│      ],
│      "verdict": {
│        "status": "matched",
│        "reasons": [],
│        "latencyMs": 5000,
│        "checkedAt": "2025-11-21T10:10:00Z",
│        "primaryObservedIndex": 0
│      }
│    }
│
└─ sk = "expectation#002"
     {
       "pk": "run#it-1234",
       "sk": "expectation#002",
       "testRunId": "it-1234",
       "serviceName": "workflowrunmanager",
       "expected": {
         "detailType": "WorkflowRunUpdated"
       },
       "observedEvents": [
         {
           "eventId": "evt-2",
           "detailType": "WorkflowRunUpdated",
           "receivedAt": "2025-11-21T10:00:15Z",
           "payloadHash": "def456",
           "rawS3Key": "events/testruns/it-1234/2025/11/21/2025-11-21T10-00-15Z-evt-2.json",
           "matchReason": "detailType"
         }
       ],
       "verdict": {
         "status": "matched",
         "reasons": [],
         "latencyMs": 15000,
         "checkedAt": "2025-11-21T10:10:00Z",
         "primaryObservedIndex": 0
       }
     }
```

### Run Meta Item (Summary)

| Attribute       | Type | Description                                                  |
|----------------|------|--------------------------------------------------------------|
| `pk`           | S    | `run#<testRunId>`                                            |
| `sk`           | S    | `run#meta`                                                   |
| `runId`        | S    | Same as `<testRunId>`                                       |
| `serviceName`  | S    | Effective service scenario used (`all`, `workflowrunmanager`, etc.) |
| `expectedSlots`| N    | Number of expectation items for this run                     |
| `observedCount`| N    | Number of expectations with at least one observed event      |
| `status`       | S    | `running`, `ready`, `timeout`, `passed`, or `failed`         |
| `startedAt`    | S    | ISO timestamp when Seeder started the run                    |
| `timeoutAt`    | S    | ISO timestamp after which the run is considered timed-out    |
| `reportS3Key`  | S    | (optional) S3 key of the HTML report                         |
| `ttl`          | N    | (optional) epoch seconds for automatic expiration            |

### Expectation Item

| Attribute          | Type | Description                                                 |
|--------------------|------|-------------------------------------------------------------|
| `pk`               | S    | `run#<testRunId>`                                           |
| `sk`               | S    | `expectation#<id>`                                          |
| `testRunId`        | S    | The associated run ID                                       |
| `serviceName`      | S    | Service scenario                                            |
| `expected`         | M    | Fixture definition (e.g. `detailType`, `payloadHash`)      |
| `observedEvents`   | L    | List of observed event summaries (from Collector)          |
| `verdict`          | M    | Per-expectation verdict (from Verifier)                    |

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
