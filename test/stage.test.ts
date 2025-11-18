import { App, Aspects, Stack } from 'aws-cdk-lib';
import { Annotations, Match } from 'aws-cdk-lib/assertions';
import { SynthesisMessage } from 'aws-cdk-lib/cx-api';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag';
import { IntegrationTestsHarnessStack } from '../infrastructure/stage/harness-stack';
import { IntegrationTestsStorageStack } from '../infrastructure/stage/storage-stack';
import {
  getIntegrationTestsHarnessStackProps,
  getIntegrationTestsStorageStackProps,
} from '../infrastructure/stage/config';

function synthesisMessageToString(sm: SynthesisMessage): string {
  return `${sm.entry.data} [${sm.id}]`;
}

describe('cdk-nag-integration-tests-stacks', () => {
  const app = new App({});

  // You should configure all stack (stateless, stateful) to be tested
  // Pick the PROD environment to test as it is the most strict
  const harnessStack = new IntegrationTestsHarnessStack(app, 'IntegrationTestsHarnessStack', {
    ...getIntegrationTestsHarnessStackProps('PROD'),
    env: {
      account: '123456789',
      region: 'ap-southeast-2',
    },
  });
  const storageStack = new IntegrationTestsStorageStack(app, 'IntegrationTestsStorageStack', {
    ...getIntegrationTestsStorageStackProps('PROD'),
    env: {
      account: '123456789',
      region: 'ap-southeast-2',
    },
  });

  Aspects.of(harnessStack).add(new AwsSolutionsChecks());
  Aspects.of(storageStack).add(new AwsSolutionsChecks());
  applyNagSuppression(harnessStack, 'IntegrationTestsHarnessStack');
  applyNagSuppression(storageStack, 'IntegrationTestsStorageStack');

  test(`cdk-nag AwsSolutions Pack errors`, () => {
    const harnessErrors = Annotations.fromStack(harnessStack)
      .findError('*', Match.stringLikeRegexp('AwsSolutions-.*'))
      .map(synthesisMessageToString);
    expect(harnessErrors).toHaveLength(0);

    const storageErrors = Annotations.fromStack(storageStack)
      .findError('*', Match.stringLikeRegexp('AwsSolutions-.*'))
      .map(synthesisMessageToString);
    expect(storageErrors).toHaveLength(0);
  });

  test(`cdk-nag AwsSolutions Pack warnings`, () => {
    const harnessWarnings = Annotations.fromStack(harnessStack)
      .findWarning('*', Match.stringLikeRegexp('AwsSolutions-.*'))
      .map(synthesisMessageToString);
    expect(harnessWarnings).toHaveLength(0);

    const storageWarnings = Annotations.fromStack(storageStack)
      .findWarning('*', Match.stringLikeRegexp('AwsSolutions-.*'))
      .map(synthesisMessageToString);
    expect(storageWarnings).toHaveLength(0);
  });
});

/**
 * apply nag suppression
 * @param stack
 * @param stackName
 */
function applyNagSuppression(stack: Stack, stackName: string) {
  NagSuppressions.addStackSuppressions(
    stack,
    [{ id: 'AwsSolutions-S10', reason: 'not require requests to use SSL' }],
    true
  );
  // FIXME one day we should remove this `AwsSolutions-IAM4` suppression and tackle any use of AWS managed policies
  //  in all our stacks. See https://github.com/umccr/orcabus/issues/174
  NagSuppressions.addStackSuppressions(
    stack,
    [{ id: 'AwsSolutions-IAM4', reason: 'allow to use AWS managed policy' }],
    true
  );
  NagSuppressions.addStackSuppressions(
    stack,
    [
      {
        id: 'AwsSolutions-APIG1',
        reason: 'See https://github.com/aws/aws-cdk/issues/11100',
      },
    ],
    true
  );
  NagSuppressions.addStackSuppressions(
    stack,
    [
      {
        id: 'AwsSolutions-APIG4',
        reason: 'We have the default Cognito UserPool authorizer',
      },
    ],
    true
  );
  // NOTE
  // This `AwsSolutions-L1` is tricky. Typically, it is okay to have one version less of the latest runtime
  // version. Not every dependency (including transitive packages) aren't upto speed with latest runtime.
  NagSuppressions.addStackSuppressions(
    stack,
    [
      {
        id: 'AwsSolutions-L1',
        reason:
          'Use the latest available runtime for the targeted language to avoid technical debt. ' +
          'Runtimes specific to a language or framework version are deprecated when the version ' +
          'reaches end of life. This rule only applies to non-container Lambda functions.',
      },
    ],
    true
  );
  NagSuppressions.addStackSuppressions(
    stack,
    [
      {
        id: 'AwsSolutions-IAM5',
        reason: 'Allow wildcard permissions based on service requirements.',
      },
    ],
    true
  );
  // Apply Step Functions suppressions only to harness stack
  if (stackName === 'IntegrationTestsHarnessStack') {
    try {
      NagSuppressions.addResourceSuppressionsByPath(
        stack,
        `/${stackName}/StepFunctionsStateMachine/Resource`,
        [
          {
            id: 'AwsSolutions-SF1',
            reason: 'CloudWatch logs are configured for Step Functions state machine.',
          },
        ],
        true
      );
      NagSuppressions.addResourceSuppressionsByPath(
        stack,
        `/${stackName}/StepFunctionsStateMachine/Resource`,
        [
          {
            id: 'AwsSolutions-SF2',
            reason: 'X-Ray tracing is enabled for Step Functions state machine.',
          },
        ],
        true
      );
    } catch {
      // Suppress error if resource path doesn't match (resource might not exist or path is different)
      // This can happen during synthesis if the resource structure is different
    }
  }

  // Apply storage-specific suppressions only to storage stack
  if (stackName === 'IntegrationTestsStorageStack') {
    try {
      // S3 bucket server access logs suppression
      NagSuppressions.addResourceSuppressionsByPath(
        stack,
        `/${stackName}/PlatformItStoreS3/Resource`,
        [
          {
            id: 'AwsSolutions-S1',
            reason: 'Server access logs not required for integration test storage bucket.',
          },
        ],
        true
      );
      // DynamoDB Point-in-time Recovery suppression
      NagSuppressions.addResourceSuppressionsByPath(
        stack,
        `/${stackName}/PlatformItStoreDynamoDB/Resource`,
        [
          {
            id: 'AwsSolutions-DDB3',
            reason: 'Point-in-time Recovery not required for integration test storage table.',
          },
        ],
        true
      );
    } catch {
      // Suppress error if resource path doesn't match (resource might not exist or path is different)
      // This can happen during synthesis if the resource structure is different
    }
  }
}
