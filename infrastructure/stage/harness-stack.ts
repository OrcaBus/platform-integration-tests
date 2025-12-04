import * as cdk from 'aws-cdk-lib';
import path from 'path';
import { Construct } from 'constructs';
import { PythonFunction, PythonLayerVersion } from '@aws-cdk/aws-lambda-python-alpha';
import { aws_lambda, Duration, Stack } from 'aws-cdk-lib';
import { ISecurityGroup, IVpc, SecurityGroup, Vpc, VpcLookupOptions } from 'aws-cdk-lib/aws-ec2';
import { EventBus, IEventBus, Rule } from 'aws-cdk-lib/aws-events';
import { LambdaFunction } from 'aws-cdk-lib/aws-events-targets';
import { Architecture } from 'aws-cdk-lib/aws-lambda';
import { ITable, Table } from 'aws-cdk-lib/aws-dynamodb';
import { IBucket, Bucket } from 'aws-cdk-lib/aws-s3';
import {
  StateMachine,
  StateMachineType,
  Pass,
  Wait,
  WaitTime,
  Choice,
  Condition,
  IChainable,
  LogLevel,
  JsonPath,
  DefinitionBody,
  TaskInput,
} from 'aws-cdk-lib/aws-stepfunctions';
import { LambdaInvoke } from 'aws-cdk-lib/aws-stepfunctions-tasks';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { PolicyStatement } from 'aws-cdk-lib/aws-iam';

export interface IntegrationTestsHarnessStackProps {
  mainBusName: string;
  vpcProps: VpcLookupOptions;
  lambdaSecurityGroupName: string;
  dynamoDBTableName: string;
  s3BucketName: string;
}
export class IntegrationTestsHarnessStack extends Stack {
  private readonly baseLayer: PythonLayerVersion;
  private readonly lambdaEnv: { [key: string]: string };
  private readonly lambdaRuntimePythonVersion: aws_lambda.Runtime = aws_lambda.Runtime.PYTHON_3_12;
  private readonly vpc: IVpc;
  private readonly lambdaSG: ISecurityGroup;
  private readonly mainBus: IEventBus;
  private readonly dynamoDBTable: ITable;
  private readonly s3Bucket: IBucket;
  private readonly serviceName: string = 'PlatformIt';

  constructor(
    scope: Construct,
    id: string,
    props: cdk.StackProps & IntegrationTestsHarnessStackProps
  ) {
    super(scope, id, props);

    this.mainBus = EventBus.fromEventBusName(this, 'OrcaBusMain', props.mainBusName);
    this.dynamoDBTable = Table.fromTableArn(
      this,
      'PlatformItStoreDynamoDB',
      `arn:aws:dynamodb:${this.region}:${this.account}:table/${props.dynamoDBTableName}`
    );
    this.s3Bucket = Bucket.fromBucketArn(
      this,
      'PlatformItStoreS3',
      `arn:aws:s3:::${props.s3BucketName}`
    );
    this.vpc = Vpc.fromLookup(this, 'MainVpc', props.vpcProps);
    this.lambdaSG = SecurityGroup.fromLookupByName(
      this,
      'LambdaSecurityGroup',
      props.lambdaSecurityGroupName,
      this.vpc
    );

    this.lambdaEnv = {
      EVENT_BUS_NAME: this.mainBus.eventBusName,
      TABLE_NAME: props.dynamoDBTableName,
      S3_BUCKET: props.s3BucketName,
    };

    this.baseLayer = new PythonLayerVersion(this, this.stackName + 'BaseLayer', {
      entry: path.join(__dirname, '../../app/deps'),
      compatibleRuntimes: [this.lambdaRuntimePythonVersion],
      compatibleArchitectures: [Architecture.ARM_64],
    });

    const seeder = this.createSeederFunction();
    const collector = this.createCollectorFunction();
    const verifier = this.createVerifierFunction();
    const reporter = this.createReporterFunction();

    // Permissioins
    this.mainBus.grantPutEventsTo(seeder);
    this.dynamoDBTable.grantReadWriteData(seeder);
    this.s3Bucket.grantRead(seeder);

    // Create disabled collector rule
    const collectorRule = this.setupCollectorEventRule(collector);
    // RuleController Lambda
    const ruleController = this.createRuleControllerFunction();

    this.dynamoDBTable.grantReadWriteData(collector);
    this.s3Bucket.grantReadWrite(collector);
    this.dynamoDBTable.grantReadWriteData(verifier);
    this.s3Bucket.grantRead(verifier);
    this.dynamoDBTable.grantReadData(reporter);
    this.s3Bucket.grantReadWrite(reporter);

    // Allow RuleController to enable/disable the collector rule
    ruleController.addToRolePolicy(
      new PolicyStatement({
        actions: ['events:EnableRule', 'events:DisableRule'],
        resources: [collectorRule.ruleArn],
      })
    );

    this.createStepFunctionsStateMachine(seeder, collector, verifier, reporter, ruleController);
  }
  private createPythonFunction(name: string, props: object): PythonFunction {
    return new PythonFunction(this, name, {
      entry: path.join(__dirname, '../../app/service/'),
      runtime: this.lambdaRuntimePythonVersion,
      layers: [this.baseLayer],
      environment: this.lambdaEnv,
      securityGroups: [this.lambdaSG],
      vpc: this.vpc,
      vpcSubnets: { subnets: this.vpc.privateSubnets },
      architecture: Architecture.ARM_64,
      ...props,
    });
  }

  private createSeederFunction(): PythonFunction {
    return this.createPythonFunction('Seeder', {
      index: 'seeder.py',
      handler: 'handler',
      timeout: Duration.seconds(300),
    });
  }

  private createCollectorFunction(): PythonFunction {
    return this.createPythonFunction('Collector', {
      index: 'collector.py',
      handler: 'handler',
      timeout: Duration.seconds(300),
    });
  }

  private setupCollectorEventRule(collector: PythonFunction): Rule {
    return new Rule(this, this.stackName + 'CollectorEventRule', {
      eventBus: this.mainBus,
      // rule name restriction: https://docs.aws.amazon.com/eventbridge/latest/APIReference/API_Rule.html
      ruleName: this.serviceName + 'CollectorEventRule',
      description: 'Rule to collect events from the main bus for the integration tests.',
      eventPattern: {
        account: [String(Stack.of(this).account)],
        // @ts-expect-error anything-but is not supported in the type definition
        source: [{ 'anything-but': 'orcabus.integrationtest' }],
      },
      enabled: false,
      targets: [
        new LambdaFunction(collector, {
          maxEventAge: Duration.seconds(60),
          retryAttempts: 3,
        }),
      ],
    });
  }

  private createRuleControllerFunction(): PythonFunction {
    return this.createPythonFunction('RuleController', {
      index: 'rule_controller.py',
      handler: 'handler',
      timeout: Duration.seconds(60),
      // override base env to add RULE_NAME for this function
      environment: {
        ...this.lambdaEnv,
        RULE_NAME: this.serviceName + 'CollectorEventRule',
      },
    });
  }

  private createVerifierFunction(): PythonFunction {
    return this.createPythonFunction('Verifier', {
      index: 'verifier.py',
      handler: 'handler',
      timeout: Duration.seconds(300),
    });
  }
  private createReporterFunction(): PythonFunction {
    return this.createPythonFunction('Reporter', {
      index: 'reporter.py',
      handler: 'handler',
      timeout: Duration.seconds(300),
    });
  }

  private createStepFunctionsStateMachine(
    seeder: PythonFunction,
    collector: PythonFunction,
    verifier: PythonFunction,
    reporter: PythonFunction,
    ruleController: PythonFunction
  ): StateMachine {
    const logGroup = new LogGroup(this, 'IntegrationTestsHarnessStateMachineLogs', {
      retention: RetentionDays.ONE_MONTH,
    });

    return new StateMachine(this, 'StepFunctionsStateMachine', {
      definitionBody: DefinitionBody.fromChainable(
        this.createStepFunctionsControllerFunction(
          seeder,
          collector,
          verifier,
          reporter,
          ruleController
        )
      ),
      stateMachineType: StateMachineType.STANDARD,
      stateMachineName: 'PlatformItStepFunctionsStateMachine',
      timeout: Duration.minutes(10),
      tracingEnabled: true,
      logs: {
        destination: logGroup,
        level: LogLevel.ALL,
      },
    });
  }

  private createStepFunctionsControllerFunction(
    seeder: PythonFunction,
    collector: PythonFunction,
    verifier: PythonFunction,
    reporter: PythonFunction,
    ruleController: PythonFunction
  ): IChainable {
    // -------------------------
    // Step Functions Definition
    // -------------------------

    // 1. RuleController: Enable/disable the collector rule
    const enableRuleTask = new LambdaInvoke(this, 'EnableRule', {
      lambdaFunction: ruleController,
      payload: TaskInput.fromObject({
        action: 'enable',
      }),
    });

    // 2. SeedScenario: Seeder will create testRunId + meta + slot items, and emit seed events.
    // Seeder is responsible for generating and returning `testRunId`.
    const seedScenarioTask = new LambdaInvoke(this, 'SeedScenario', {
      lambdaFunction: seeder,
      payloadResponseOnly: true,
      // Seeder returns { testRunId, scenario, expectedSlots, ... }
      // We store that under $.seedResult
      resultPath: '$.seedResult',
    });

    // 3. CheckRunStatus: call Verifier in "status" mode
    // Input to verifier:
    //   { "testRunId": <from seedResult>, "mode": "status" }
    const checkRunStatusTask = new LambdaInvoke(this, 'CheckRunStatus', {
      lambdaFunction: verifier,
      payload: TaskInput.fromObject({
        testRunId: JsonPath.stringAt('$.seedResult.testRunId'),
        mode: 'status',
      }),
      payloadResponseOnly: true,
      // Expect verifier to return:
      // { status: "running|ready|timeout", runId: "...", observedCount, expectedCount }
      resultPath: '$.status',
    });

    // 4. Wait X seconds
    const waitX = new Wait(this, 'WaitForEvents', {
      time: WaitTime.duration(Duration.minutes(1)),
    });

    // 5. Verify: call Verifier in "verify" mode once ready/timeout
    const verifyTask = new LambdaInvoke(this, 'VerifyRun', {
      lambdaFunction: verifier,
      payload: TaskInput.fromObject({
        testRunId: JsonPath.stringAt('$.seedResult.testRunId'),
        mode: 'verify',
      }),
      payloadResponseOnly: true,
      // Verifier returns e.g. { runId, runStatus, matchedCount, missingCount, unexpectedCount, totalExpected }
      resultPath: '$.verifyResult',
    });

    // 6. Report: Reporter builds HTML, stores in S3
    const reportTask = new LambdaInvoke(this, 'ReportRun', {
      lambdaFunction: reporter,
      payload: TaskInput.fromObject({
        testRunId: JsonPath.stringAt('$.seedResult.testRunId'),
        verifyResult: JsonPath.stringAt('$.verifyResult'),
        // safe if you always document that callers pass serviceName or Seeder sets it in seedResult
        serviceName: JsonPath.stringAt('$.seedResult.serviceName'),
      }),
      payloadResponseOnly: true,
      resultPath: '$.reportResult',
    });

    // 7. Disable Collector rule after run finishes
    const disableCollectorTask = new LambdaInvoke(this, 'DisableCollectorRule', {
      lambdaFunction: ruleController,
      payload: TaskInput.fromObject({
        action: 'disable',
      }),
      payloadResponseOnly: true,
      resultPath: JsonPath.DISCARD,
    });

    // 8. Final state
    const finalState = new Pass(this, 'Done', {
      resultPath: '$.final',
    });

    // verify -> report -> disable -> done chain (only when ready/timeout)
    const verifyAndReportChain = verifyTask
      .next(reportTask)
      .next(disableCollectorTask)
      .next(finalState);

    // Choice based on status.status
    const statusChoice = new Choice(this, 'RunReadyOrTimeout?')
      .when(Condition.stringEquals('$.status.status', 'ready'), verifyAndReportChain)
      .when(Condition.stringEquals('$.status.status', 'timeout'), verifyAndReportChain)
      // Otherwise, wait again
      .otherwise(waitX);

    // Loop: wait -> check status -> (if ready/timeout: verify -> report -> disable, else: wait again)
    const waitLoop = waitX.next(checkRunStatusTask).next(statusChoice);

    // Main chain: enable rule -> seed -> loop
    return enableRuleTask.next(seedScenarioTask).next(waitLoop);
  }
}
