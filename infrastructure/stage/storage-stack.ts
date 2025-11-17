import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { Stack, RemovalPolicy } from 'aws-cdk-lib';
import { StageName } from '@orcabus/platform-cdk-constructs/shared-config/accounts';
import { Bucket } from 'aws-cdk-lib/aws-s3';
import { Table, AttributeType } from 'aws-cdk-lib/aws-dynamodb';
import { BillingMode } from 'aws-cdk-lib/aws-dynamodb';

export interface IntegrationTestsStorageStackProps {
  readonly stage: StageName;
  readonly bucketName: string;
  readonly tableName: string;
}

export class IntegrationTestsStorageStack extends Stack {
  constructor(
    scope: Construct,
    id: string,
    props?: cdk.StackProps & IntegrationTestsStorageStackProps
  ) {
    super(scope, id, props);

    // --- Storage ---
    new Table(this, 'PlatformItStoreDynamoDB', {
      tableName: props?.tableName,
      partitionKey: { name: 'testId', type: AttributeType.STRING },
      sortKey: { name: 'sk', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: RemovalPolicy.DESTROY,
    });

    new Bucket(this, `PlatformItStoreS3`, {
      bucketName: props?.bucketName,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });
  }
}
