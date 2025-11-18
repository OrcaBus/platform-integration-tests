import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { getIntegrationTestsHarnessStackProps } from '../stage/config';
import { IntegrationTestsHarnessStack } from '../stage/harness-stack';
import { IntegrationTestDeploymentStackPipeline } from './DeploymentStackPipeline';

export class StatelessStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    new IntegrationTestDeploymentStackPipeline(this, 'DeploymentPipeline', {
      githubBranch: 'main',
      githubRepo: 'platform-integration-tests',
      stack: IntegrationTestsHarnessStack,
      stackName: /** TODO: Replace with string. Example: */ 'StatelessMicroserviceManager',
      stackConfig: {
        beta: getIntegrationTestsHarnessStackProps('BETA'),
        gamma: getIntegrationTestsHarnessStackProps('GAMMA'),
      },
      pipelineName: /** TODO: Replace with string. Example: */ 'OrcaBus-StatelessMicroservice',
      cdkSynthCmd: ['pnpm install --frozen-lockfile --ignore-scripts', 'pnpm cdk-stateless synth'],
    });
  }
}
