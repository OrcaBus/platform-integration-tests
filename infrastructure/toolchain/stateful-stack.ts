import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { IntegrationTestDeploymentStackPipeline } from './DeploymentStackPipeline';
import { getIntegrationTestsStorageStackProps } from '../stage/config';
import { IntegrationTestsStorageStack } from '../stage/storage-stack';

export class StatefulStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    new IntegrationTestDeploymentStackPipeline(this, 'DeploymentPipeline', {
      githubBranch: 'main',
      githubRepo: 'platform-integration-tests',
      stack: IntegrationTestsStorageStack,
      stackName: 'StatefulPlatformItStorageStack',
      stackConfig: {
        gamma: getIntegrationTestsStorageStackProps('GAMMA'),
      },
      pipelineName: 'StatefulPlatformItStoragePipeline',
      cdkSynthCmd: ['pnpm install --frozen-lockfile --ignore-scripts', 'pnpm cdk-stateful synth'],
    });
  }
}
