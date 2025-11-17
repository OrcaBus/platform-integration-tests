import * as cdk from 'aws-cdk-lib';
import path from 'path';
import { Construct } from 'constructs';
import { Role } from 'aws-cdk-lib/aws-iam';
import { LambdaFunction } from 'aws-cdk-lib/aws-events-targets';
import { PythonFunction, PythonLayerVersion } from '@aws-cdk/aws-lambda-python-alpha';
import { aws_lambda, Duration, Stack } from 'aws-cdk-lib';
import { ISecurityGroup, IVpc, SecurityGroup, Vpc, VpcLookupOptions } from 'aws-cdk-lib/aws-ec2';
import { EventBus, IEventBus, Rule } from 'aws-cdk-lib/aws-events';
import { Architecture } from 'aws-cdk-lib/aws-lambda';

export interface IntegrationTestsOrchestratorStackProps {
  mainBusName: string;
  vpcProps: VpcLookupOptions;
  lambdaSecurityGroupName: string;
  tableName: string;
  s3BucketName: string;
}
export class IntegrationTestsOrchestratorStack extends Stack {
  private readonly baseLayer: PythonLayerVersion;
  private readonly lambdaEnv;
  private readonly lambdaRuntimePythonVersion: aws_lambda.Runtime = aws_lambda.Runtime.PYTHON_3_12;
  private readonly lambdaRole: Role;
  private readonly vpc: IVpc;
  private readonly lambdaSG: ISecurityGroup;
  private readonly mainBus: IEventBus;

  constructor(
    scope: Construct,
    id: string,
    props: cdk.StackProps & IntegrationTestsOrchestratorStackProps
  ) {
    super(scope, id, props);

    this.mainBus = EventBus.fromEventBusName(this, 'OrcaBusMain', props.mainBusName);
    this.vpc = Vpc.fromLookup(this, 'MainVpc', props.vpcProps);
    this.lambdaSG = SecurityGroup.fromLookupByName(
      this,
      'LambdaSecurityGroup',
      props.lambdaSecurityGroupName,
      this.vpc
    );

    this.lambdaEnv = {
      EVENT_BUS_NAME: this.mainBus.eventBusName,
      TABLE_NAME: props.tableName,
      S3_BUCKET: props.s3BucketName,
    };

    this.baseLayer = new PythonLayerVersion(this, this.stackName + 'BaseLayer', {
      entry: path.join(__dirname, '../../app/deps'),
      compatibleRuntimes: [this.lambdaRuntimePythonVersion],
      compatibleArchitectures: [Architecture.ARM_64],
    });

    const collector = this.createPythonFunction('Collector', {
      handler: 'handler',
      timeout: Duration.seconds(300),
    });

    const seeder = this.createPythonFunction('Seeder', {
      handler: 'handler',
      timeout: Duration.seconds(300),
    });

    new Rule(this, 'CollectorRule', {
      eventBus: this.mainBus,
      eventPattern: {
        source: ['platform-integration-tests.seeder'],
      },
      targets: [new LambdaFunction(collector)],
    });

    new Rule(this, 'SeederRule', {
      eventBus: this.mainBus,
      eventPattern: {
        source: ['platform-integration-tests.seeder'],
      },
      targets: [new LambdaFunction(seeder)],
    });
  }
  private createPythonFunction(name: string, props: object): PythonFunction {
    return new PythonFunction(this, name, {
      entry: path.join(__dirname, '../../app/service/', name + '.py'),
      runtime: this.lambdaRuntimePythonVersion,
      layers: [this.baseLayer],
      environment: this.lambdaEnv,
      securityGroups: [this.lambdaSG],
      vpc: this.vpc,
      vpcSubnets: { subnets: this.vpc.privateSubnets },
      role: this.lambdaRole,
      architecture: Architecture.ARM_64,
      ...props,
    });
  }
}
