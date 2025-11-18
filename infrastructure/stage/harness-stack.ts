import * as cdk from 'aws-cdk-lib';
import path from 'path';
import { Construct } from 'constructs';
import { PythonFunction, PythonLayerVersion } from '@aws-cdk/aws-lambda-python-alpha';
import { aws_lambda, Duration, Stack } from 'aws-cdk-lib';
import { ISecurityGroup, IVpc, SecurityGroup, Vpc, VpcLookupOptions } from 'aws-cdk-lib/aws-ec2';
import { EventBus, IEventBus } from 'aws-cdk-lib/aws-events';
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
} from 'aws-cdk-lib/aws-stepfunctions';
import { LambdaInvoke } from 'aws-cdk-lib/aws-stepfunctions-tasks';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';

export interface IntegrationTestsHarnessStackProps {
  mainBusName: string;
  vpcProps: VpcLookupOptions;
  lambdaSecurityGroupName: string;
  dynamoDBTableName: string;
  s3BucketName: string;
}
export class IntegrationTestsHarnessStack extends Stack {
  private readonly baseLayer: PythonLayerVersion;
  private readonly lambdaEnv;
  private readonly lambdaRuntimePythonVersion: aws_lambda.Runtime = aws_lambda.Runtime.PYTHON_3_12;
  private readonly vpc: IVpc;
  private readonly lambdaSG: ISecurityGroup;
  private readonly mainBus: IEventBus;
  private readonly dynamoDBTable: ITable;
  private readonly s3Bucket: IBucket;

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
    this.dynamoDBTable.grantReadWriteData(collector);
    this.s3Bucket.grantReadWrite(collector);
    this.dynamoDBTable.grantReadWriteData(verifier);
    this.dynamoDBTable.grantReadData(reporter);
    this.s3Bucket.grantReadWrite(reporter);

    this.createStepFunctionsStateMachine(seeder, collector, verifier, reporter);
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
    reporter: PythonFunction
  ): StateMachine {
    const logGroup = new LogGroup(this, 'IntegrationTestsHarnessStateMachineLogs', {
      retention: RetentionDays.ONE_MONTH,
    });

    return new StateMachine(this, 'StepFunctionsStateMachine', {
      definition: this.createStepFunctionsControllerFunction(seeder, collector, verifier, reporter),
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
    reporter: PythonFunction
  ): IChainable {
    // -------------------------
    // Step Functions Definition
    // -------------------------

    // 1. GenerateRunId
    // For now this Pass just forwards input; runId is expected from caller or Seeder's output.
    const generateRunId = new Pass(this, 'GenerateRunId', {
      resultPath: '$',
    });

    // 2. SeedScenario: Seeder will create run#meta + slot items, and emit seed events.
    const seedScenarioTask = new LambdaInvoke(this, 'SeedScenario', {
      lambdaFunction: seeder,
      payloadResponseOnly: true,
      // Seeder returns { runId, scenario, expectedSlots, ... }
      // We store that under $.seedResult
      resultPath: '$.seedResult',
    });

    // 3. CheckRunStatus: call Verifier in "status" mode
    // Input to verifier:
    //   { "runId": <from seedResult>, "mode": "status" }
    const checkRunStatusTask = new LambdaInvoke(this, 'CheckRunStatus', {
      lambdaFunction: verifier,
      payload: cdk.aws_stepfunctions.TaskInput.fromObject({
        runId: JsonPath.stringAt('$.seedResult.runId'),
        mode: 'status',
      }),
      payloadResponseOnly: true,
      // Expect verifier to return:
      // { status: "running|ready|timeout", runId: "...", observedCount, expectedSlots }
      resultPath: '$.status',
    });

    // 4. Wait X seconds
    const waitX = new Wait(this, 'WaitForEvents', {
      time: WaitTime.duration(Duration.seconds(5)),
    });

    // 5. Verify: call Verifier in "verify" mode once ready/timeout
    const verifyTask = new LambdaInvoke(this, 'VerifyRun', {
      lambdaFunction: verifier,
      payload: cdk.aws_stepfunctions.TaskInput.fromObject({
        runId: JsonPath.stringAt('$.seedResult.runId'),
        mode: 'verify',
      }),
      payloadResponseOnly: true,
      // Verifier returns e.g. { runId, runStatus, slotStatusCounts }
      resultPath: '$.verifyResult',
    });

    // 6. Report: Reporter builds HTML, stores in S3
    const reportTask = new LambdaInvoke(this, 'ReportRun', {
      lambdaFunction: reporter,
      payload: cdk.aws_stepfunctions.TaskInput.fromObject({
        runId: JsonPath.stringAt('$.seedResult.runId'),
        verifyResult: JsonPath.stringAt('$.verifyResult'),
      }),
      payloadResponseOnly: true,
      resultPath: '$.reportResult',
    });

    // 7. Final state
    const finalState = new Pass(this, 'Done', {
      resultPath: '$.final',
    });

    // verify -> report -> done chain
    const verifyReportChain = verifyTask.next(reportTask).next(finalState);

    // Choice based on status.status
    const statusChoice = new Choice(this, 'RunReadyOrTimeout?')
      .when(Condition.stringEquals('$.status.status', 'ready'), verifyReportChain)
      .when(Condition.stringEquals('$.status.status', 'timeout'), verifyReportChain)
      // Otherwise, wait again
      .otherwise(waitX);

    const waitLoop = waitX.next(checkRunStatusTask).next(statusChoice);

    // Main chain
    return generateRunId.next(seedScenarioTask).next(waitLoop);
  }
}
