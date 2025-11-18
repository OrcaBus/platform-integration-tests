import { StageName } from '@orcabus/platform-cdk-constructs/shared-config/accounts';
import { IntegrationTestsStorageStackProps } from './storage-stack';
import { IntegrationTestsHarnessStackProps } from './harness-stack';
import {
  BETA_ENVIRONMENT,
  GAMMA_ENVIRONMENT,
} from '@orcabus/platform-cdk-constructs/deployment-stack-pipeline';
import {
  VPC_LOOKUP_PROPS,
  SHARED_SECURITY_GROUP_NAME,
} from '@orcabus/platform-cdk-constructs/shared-config/networking';
import { EVENT_BUS_NAME } from '@orcabus/platform-cdk-constructs/shared-config/event-bridge';

export const getIntegrationTestsHarnessStackProps = (
  stage: StageName
): IntegrationTestsHarnessStackProps => {
  const accountId = stage === 'BETA' ? BETA_ENVIRONMENT.account : GAMMA_ENVIRONMENT.account;
  const region = stage === 'BETA' ? BETA_ENVIRONMENT.region : GAMMA_ENVIRONMENT.region;
  const stageLower = stage.toLowerCase();
  return {
    dynamoDBTableName: `orcabus-platform-it-${stageLower}-store`,
    s3BucketName: `orcabus-platform-it-${stageLower}-${accountId}-${region}`,
    vpcProps: VPC_LOOKUP_PROPS,
    lambdaSecurityGroupName: SHARED_SECURITY_GROUP_NAME,
    mainBusName: EVENT_BUS_NAME,
  };
};

export const getIntegrationTestsStorageStackProps = (
  stage: StageName
): IntegrationTestsStorageStackProps => {
  const accountId = stage === 'BETA' ? BETA_ENVIRONMENT.account : GAMMA_ENVIRONMENT.account;
  const region = stage === 'BETA' ? BETA_ENVIRONMENT.region : GAMMA_ENVIRONMENT.region;
  const stageLower = stage.toLowerCase();
  return {
    stage: stage,
    bucketName: `orcabus-platform-it-${stageLower}-${accountId}-${region}`,
    dynamoDBTableName: `orcabus-platform-it-${stageLower}-store`,
  };
};
