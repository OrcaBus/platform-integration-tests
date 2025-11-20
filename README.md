platform-integration-tests
================================================================================

- [platform-integration-tests](#platform-integration-tests)
  - [Service Description](#service-description)
    - [Responsibility](#responsibility)
    - [Architecture](#architecture)
    - [System Component](#system-component)
    - [CI/CD Integration](#cicd-integration)
  - [Infrastructure \& Deployment](#infrastructure--deployment)
    - [Stateful](#stateful)
    - [Stateless](#stateless)
    - [CDK Commands](#cdk-commands)
    - [Stacks](#stacks)
  - [Development](#development)
    - [Project Structure](#project-structure)
    - [Setup](#setup)
      - [Requirements](#requirements)
      - [Install Dependencies](#install-dependencies)
      - [First Steps](#first-steps)
    - [Linting \& Formatting](#linting--formatting)
    - [Testing](#testing)
  - [Glossary \& References](#glossary--references)


Service Description
--------------------------------------------------------------------------------

## Responsibility

**Staging guardrail for OrcaBus** — a fast, deterministic integration-testing system that exercises **real orchestration** on the OrcaBus **staging EventBridge bus** without running expensive external workloads. It seeds scenarios, collects emitted events, verifies them against golden fixtures, and emits a single **pass/fail verdict** to gate production in CI/CD.

## Architecture

![Architecture](./docs/Integration_Testing.drawio.svg)


**Key points**
- Exactly **one Lambda per role**: `Seeder`, `Collector`, `Verifier`, `Reporter`.
- A **Step Functions controller** only orchestrates **Seeder + Collector** at start and **Verifier (+ Reporter)** at the end. It **does not** read or write DynamoDB or S3 directly.
- A **single DynamoDB table** stores **everything** per run: **run metadata**, **fixtures**, **observed events**, and the **final verdict**.


### high levelArchitecture

1. CI/CD deploys OrcaBus to **staging**.
2. CI/CD triggers the **test controller** (AWS Step Functions).
3. The controller:
   - Creates a unique `testId` and marks the run as **test mode**.
   - Seeds a scenario via the **Seeder Lambda**.
   - Collects all test-mode events via the **Collector Lambda**.
   - Waits until all expected events arrive or a timeout occurs.
   - Invokes the **Verifier Lambda** to compute a verdict.
   - Invokes the **Reporter Lambda** to publish a report.
4. CI/CD reads the verdict and **allows or blocks promotion to production**.

### AWS Services

- **AWS Step Functions**
  - Orchestrates a single integration test run end-to-end.
  - Does **not** access DynamoDB or S3 directly; all logic is in Lambdas.

- **AWS Lambda** (Python)
  - `Seeder` – seeds fixtures and initial events.
  - `Collector` – archives test-mode events for the current run.
  - `Verifier` – compares fixtures vs. observations and writes verdict.
  - `Reporter` – creates a human-readable report and optionally posts notifications.


- **Amazon DynamoDB**
  - **Single table** that stores:
    - Run metadata
    - Fixtures
    - Observed events
    - Final verdict
    - (Optional) report metadata

- **Amazon S3**
  - Stores HTML/JSON reports for each run.
  - Optionally store full-size event payloads if they are large.

## System Component

1. **Test Controller (Step Functions)**
   On trigger (from **CodePipeline** or manual):
   - Invoke **Seeder** and **Collector** (control “start”).
   - Wait/poll until the run is **ready** (all expected events seen) or **timeout**.
   - Invoke **Verifier**, then **Reporter**.

2. **Seeder (Lambda, Python)**
   Writes **fixtures (expected events)** for the scenario to DynamoDB and publishes initial **seed event(s)** with `testId`.

3. **Collector (Lambda, Python)**
   While the rule is enabled, receives **test-mode** events from EventBridge and archives **all events for `testId`** to DynamoDB (with dedupe).

4. **Verifier (Lambda, Python)**
   When **ready** or **timeout**, loads fixtures + observations and checks **presence, order, schema (optional), duplicates, latency windows**. Writes a **verdict** to DynamoDB.

5. **Reporter (Lambda, Python)**
   Reads the verdict, builds an **HTML/JSON** report to **S3**, and (optionally) posts to Slack. CI can consume the verdict to approve/block promotion.

## CI/CD Integration

Typical CodePipeline flow:

1. **Build & unit tests**
2. **Deploy OrcaBus to staging**
3. **Run integration harness** (this system)
   - Action calls the Step Functions controller with:
     - `scenario` (or multiple scenarios)
   - Waits for the SFN execution to finish.
4. **Decision**
   - If `verdict = "passed"`:
     - Proceed to production deploy.
   - If `verdict = "failed"`:
     - Fail the pipeline or require manual approval.
5. **Deploy to production (after manual approve)**



Infrastructure & Deployment
--------------------------------------------------------------------------------

Infrastructure and deployment are managed via CDK. The system uses AWS CDK to provision all required resources including Lambda functions, Step Functions state machines, DynamoDB tables, S3 buckets, and EventBridge rules. This template provides two types of CDK entry points: `cdk-stateless` and `cdk-stateful`.


### Stateful

Stateful resources that persist data across deployments:

- **DynamoDB table** (`platform-it-store`): Stores run metadata, fixtures, observed events, and verdicts
- **S3 buckets** (`platform-it-store`): Stores full event payloads and test reports

### Stateless
Stateless resources that can be redeployed without side effects:

- **Lambda functions**: `Seeder`, `Collector`, `Verifier`, and `Reporter`
- **Step Functions state machine**: Orchestrates the test execution workflow
- **EventBridge rules**: Routes test-mode events to the Collector


### CDK Commands

You can access CDK commands using the `pnpm` wrapper script.

- **`cdk-stateless`**: Used to deploy stacks containing stateless resources (e.g., AWS Lambda), which can be easily redeployed without side effects.
- **`cdk-stateful`**: Used to deploy stacks containing stateful resources (e.g., AWS DynamoDB, AWS RDS), where redeployment may not be ideal due to potential side effects.

The type of stack to deploy is determined by the context set in the `./bin/deploy.ts` file. This ensures the correct stack is executed based on the provided context.

For example:

```sh
# Deploy a stateless stack
pnpm cdk-stateless <command>

# Deploy a stateful stack
pnpm cdk-stateful <command>
```

### Stacks

This CDK project manages multiple stacks. The root stack (the only one that does not include `DeploymentPipeline` in its stack ID) is deployed in the toolchain account and sets up a CodePipeline for cross-environment deployments to `beta`, `gamma`, and `prod`.

To list all available stateless stacks, run:

```sh
pnpm cdk-stateless ls
```

Example output:

```sh
StatelessPlatformItHarnessStack
StatelessPlatformItHarnessStack/DeploymentPipeline/OrcaBusBeta/StatelessPlatformItHarnessStack (OrcaBusBeta-StatelessPlatformItHarnessStack)
StatelessPlatformItHarnessStack/DeploymentPipeline/OrcaBusGamma/StatelessPlatformItHarnessStack (OrcaBusGamma-StatelessPlatformItHarnessStack)
```

To build the CICD pipeline for Integration Testing stateless service
```sh
pnpm cdk deploy -e StatelessPlatformItHarnessStack
```

To build (test) in the dev account
```sh
pnpm cdk synth -e StatelessPlatformItHarnessStack/DeploymentPipeline/OrcaBusBeta/StatelessPlatformItHarnessStack
pnpm cdk diff -e StatelessPlatformItHarnessStack/DeploymentPipeline/OrcaBusBeta/StatelessPlatformItHarnessStack
pnpm cdk deploy -e StatelessPlatformItHarnessStack/DeploymentPipeline/OrcaBusBeta/StatelessPlatformItHarnessStack
```

To list all available stateful stacks, run:

```sh
pnpm cdk-stateful ls
```

Example output:

```sh
StatefulPlatformItStorageStack
StatefulPlatformItStorageStack/DeploymentPipeline/OrcaBusBeta/StatefulPlatformItStorageStack (OrcaBusBeta-StatefulPlatformItStorageStack)
StatefulPlatformItStorageStack/DeploymentPipeline/OrcaBusGamma/StatefulPlatformItStorageStack (OrcaBusGamma-StatefulPlatformItStorageStack)
```

To build the CICD pipeline for Integration Testing stateful service
```sh
pnpm ccdk-stateful deploy -e StatefulPlatformItStorageStack
```

To build (test) in the dev account
```sh
pnpm cdk-stateful synth -e StatefulPlatformItStorageStack/DeploymentPipeline/OrcaBusBeta/StatefulPlatformItStorageStack
pnpm cdk-stateful diff -e StatefulPlatformItStorageStack/DeploymentPipeline/OrcaBusBeta/StatefulPlatformItStorageStack
pnpm cdk-stateful deploy -e StatefulPlatformItStorageStack/DeploymentPipeline/OrcaBusBeta/StatefulPlatformItStorageStack
```

Development
--------------------------------------------------------------------------------

### Project Structure

The root of the project is an AWS CDK project where the main application logic lives inside the `./app` folder.

The project is organized into the following key directories:

- **`./app`**: Contains the main application logic. You can open the code editor directly in this folder, and the application should run independently.

- **`./bin/deploy.ts`**: Serves as the entry point of the application. It initializes two root stacks: `stateless` and `stateful`. You can remove one of these if your service does not require it.

- **`./infrastructure`**: Contains the infrastructure code for the project:
  - **`./infrastructure/toolchain`**: Includes stacks for the stateless and stateful resources deployed in the toolchain account. These stacks primarily set up the CodePipeline for cross-environment deployments.
  - **`./infrastructure/stage`**: Defines the stage stacks for different environments:
    - **`./infrastructure/stage/config.ts`**: Contains environment-specific configuration files (e.g., `beta`, `gamma`, `prod`).
    - **`./infrastructure/stage/stack.ts`**: The CDK stack entry point for provisioning resources required by the application in `./app`.

- **`.github/workflows/pr-tests.yml`**: Configures GitHub Actions to run tests for `make check` (linting and code style), tests defined in `./test`, and `make test` for the `./app` directory. Modify this file as needed to ensure the tests are properly configured for your environment.

- **`./test`**: Contains tests for CDK code compliance against `cdk-nag`. You should modify these test files to match the resources defined in the `./infrastructure` folder.


### Setup

#### Requirements

```sh
node --version
v22.9.0

# Update Corepack (if necessary, as per pnpm documentation)
npm install --global corepack@latest

# Enable Corepack to use pnpm
corepack enable pnpm

```

#### Install Dependencies

To install all required dependencies, run:

```sh
make install
```

#### First Steps

Before using this template, search for all instances of `TODO:` comments in the codebase and update them as appropriate for your service. This includes replacing placeholder values (such as stack names).


### Linting & Formatting

Automated checks are enforced via pre-commit hooks, ensuring only checked code is committed. For details consult the `.pre-commit-config.yaml` file.

Manual, on-demand checking is also available via `make` targets (see below). For details consult the `Makefile` in the root of the project.


To run linting and formatting checks on the root project, use:

```sh
make check
```

To automatically fix issues with ESLint and Prettier, run:

```sh
make fix
```

### Testing


Unit tests are available for most of the business logic. Test code is hosted alongside business logic in `./test` directories.

```sh
make test
```

Glossary & References
--------------------------------------------------------------------------------

For general terms and expressions used across OrcaBus services, please see the platform [documentation](https://github.com/OrcaBus/wiki/blob/main/orcabus-platform/README.md#glossary--references).

Service specific terms:

| Term      | Description                                      |
|-----------|--------------------------------------------------|
| `testRunId`  | Unique identifier for a test execution run |
| Slot | A placeholder for an expected event, containing both the fixture (expected) and observed event data |
| Fixture | Expected event data that defines what should be observed during a test run |
| Verdict | The pass/fail status and reasons for a test run or individual event slot |
