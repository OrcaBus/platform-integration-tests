import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { getIntegrationTestsOrchestratorStackProps } from '../stage/config';
import { IntegrationTestsOrchestratorStack } from '../stage/orchestrator-stack';
import { IntegrationTestDeploymentStackPipeline } from './DeploymentStackPipeline';

export class StatelessStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    new IntegrationTestDeploymentStackPipeline(this, 'DeploymentPipeline', {
      githubBranch: 'main',
      githubRepo: 'platform-integration-tests',
      stack: IntegrationTestsOrchestratorStack,
      stackName: /** TODO: Replace with string. Example: */ 'StatelessMicroserviceManager',
      stackConfig: {
        beta: getIntegrationTestsOrchestratorStackProps('BETA'),
        gamma: getIntegrationTestsOrchestratorStackProps('GAMMA'),
      },
      pipelineName: /** TODO: Replace with string. Example: */ 'OrcaBus-StatelessMicroservice',
      cdkSynthCmd: ['pnpm install --frozen-lockfile --ignore-scripts', 'pnpm cdk-stateless synth'],
    });
  }
}
