/* eslint-disable @typescript-eslint/no-explicit-any */
import { Construct } from 'constructs';
import { Environment, Stack, StackProps, Stage } from 'aws-cdk-lib';
import { BuildSpec, ComputeType, LinuxArmBuildImage } from 'aws-cdk-lib/aws-codebuild';
import { StringParameter } from 'aws-cdk-lib/aws-ssm';
import { CodeBuildStep, CodePipeline, CodePipelineSource } from 'aws-cdk-lib/pipelines';
import {
  Pipeline,
  CfnPipeline,
  PipelineType,
  PipelineNotificationEvents,
} from 'aws-cdk-lib/aws-codepipeline';
import { SlackChannelConfiguration } from 'aws-cdk-lib/aws-chatbot';
import { DetailType } from 'aws-cdk-lib/aws-codestarnotifications';
import { GAMMA_ENVIRONMENT } from '@orcabus/platform-cdk-constructs/deployment-stack-pipeline';
import { CrossDeploymentArtifactBucket } from '@orcabus/platform-cdk-constructs/deployment-stack-pipeline';

/**
 * The default partial build spec for the synth step in the pipeline.
 */
export const DEFAULT_SYNTH_STEP_PARTIAL_BUILD_SPEC = {
  phases: {
    install: {
      'runtime-versions': {
        nodejs: '22.x',
      },
    },
  },
};

export interface StageEnvProps {
  /**
   * The environment for the gamma stage
   */
  readonly gamma: Environment;
}

export interface StackConfigProps {
  /**
   * The configuration for the gamma (stg) stage
   */
  readonly gamma: Record<string, any>;
}

export interface IntegrationTestDeploymentStackPipelineProps {
  /**
   * The GitHub branch name the pipeline will listen to.
   * Avoid using branch names that contain special characters such as parentheses.
   * Allowed special characters are: "+ - = . _ : / @".
   * This restriction is due to AWS resource tagging requirements.
   */
  readonly githubBranch: string;
  /**
   * The repository name that exist in the 'OrcaBus' github organisation. e.g. `a-micro-service-repo`
   */
  readonly githubRepo: string;
  /**
   * The stack to which the pipeline will be deploying to its respective account
   */
  readonly stack: any;
  /**
   * The stack name (in cloudformation) for the stack defined in `stack`. The stack name will prepend with the stage
   * name e.g. `OrcaBusBeta-<stackName>`, `OrcaBusGamma-<stackName>`, `OrcaBusProd-<stackName>`
   */
  readonly stackName: string;
  /**
   * The stack configuration/constants that will be passed to the stack props.
   */
  readonly stackConfig: StackConfigProps;
  /**
   * The pipeline name in the bastion account.
   */
  readonly pipelineName: string;
  /**
   * The list of patterns of Git repository file paths that, when a commit is pushed, are to be INCLUDED as criteria that starts the pipeline.
   *
   * https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-codepipeline-pipeline-gitfilepathfiltercriteria.html
   *
   */
  readonly includedFilePaths?: string[];
  /**
   * The list of patterns of Git repository file paths that, when a commit is pushed, are to be EXCLUDED from starting the pipeline.
   *
   * https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-codepipeline-pipeline-gitfilepathfiltercriteria.html
   *
   */
  readonly excludedFilePaths?: string[];
  /**
   * The command to run to synth the cdk stack which also installing the cdk dependencies. e.g. ["yarn install --immutable", "yarn cdk synth"]
   */
  readonly cdkSynthCmd: string[];
  /**
   * The location where the cdk output will be stored.
   *
   * @default cdk.out
   */
  readonly cdkOut?: string;
  /**
   * Additional configuration for the CodeBuild step during the CDK synth phase. It will passed as the `partialBuildSpec` to the `CodeBuildStep`.
   *
   * @default DEFAULT_SYNTH_STEP_PARTIAL_BUILD_SPEC
   */
  readonly synthBuildSpec?: Record<string, any>;
  /**
   * The stage environment for the deployment stack
   */
  readonly stageEnv?: StageEnvProps;
  /**
   * Enable notification to the 'alerts-build' slack channel.
   * @default True
   */
  readonly enableSlackNotification?: boolean;
  /**
   * The pipeline notification events that will trigger a Slack channel notification.
   * Only applies if `enableSlackNotification` is set to true.
   *
   * @default [PipelineNotificationEvents.PIPELINE_EXECUTION_FAILED] –
   *   Only failed pipeline executions will trigger a notification.
   */
  readonly notificationEvents?: PipelineNotificationEvents[];

  /**
   * Whether to reuse the existing artifact bucket for cross-deployment pipelines.
   * If set to true, it will look up the existing artifact bucket in the TOOLCHAIN account.
   *
   * @default True
   */
  readonly reuseExistingArtifactBucket?: boolean;

  /**
   * Remove assets from the CDK assembly pre-deployment to prevent hitting CodePipeline's 256 MB artifact size limit.
   * Useful when CDK assets (Lambda code, Docker images, etc.) are large.
   *
   * @see https://github.com/aws/aws-cdk/issues/9917
   */
  readonly stripAssemblyAssets?: boolean;
}

/**
 * A CDK construct that creates a deployment pipeline across environments for the OrcaBus project.
 *
 * Prerequisite: Ensure that the "CrossDeploymentArtifactBucket" stack is deployed in the TOOLCHAIN account
 * before using this construct.
 */
export class IntegrationTestDeploymentStackPipeline extends Construct {
  /**
   * The code pipeline construct that is created.
   */
  readonly pipeline: Pipeline;

  constructor(scope: Construct, id: string, props: IntegrationTestDeploymentStackPipelineProps) {
    super(scope, id);

    const codeStarArn = StringParameter.valueForStringParameter(
      this,
      '/orcabus/codestar_github_arn'
    );
    const codeStarSourceActionName = 'pipeline-src';
    const sourceFile = CodePipelineSource.connection(
      `OrcaBus/${props.githubRepo}`,
      props.githubBranch,
      {
        connectionArn: codeStarArn,
        actionName: codeStarSourceActionName,
        triggerOnPush: true,
      }
    );

    const isReusingExistingArtifactBucket = props.reuseExistingArtifactBucket ?? true;
    const artifactBucket = isReusingExistingArtifactBucket
      ? CrossDeploymentArtifactBucket.fromLookup(this).artifactBucket
      : undefined;

    this.pipeline = new Pipeline(this, 'DeploymentCodePipeline', {
      artifactBucket: artifactBucket,
      pipelineType: PipelineType.V2,
      pipelineName: props.pipelineName,
      crossAccountKeys: true,
    });

    if (props.includedFilePaths || props.excludedFilePaths) {
      const filePaths: Record<string, string[]> = {};

      if (props.includedFilePaths) {
        filePaths['Includes'] = props.includedFilePaths;
      }
      if (props.excludedFilePaths) {
        filePaths['Excludes'] = props.excludedFilePaths;
      }

      // Add event filter to only trigger if the push event is from `deploy` directory
      const cfnPipeline = this.pipeline.node.defaultChild as CfnPipeline;
      cfnPipeline.addPropertyOverride('Triggers', [
        {
          GitConfiguration: {
            Push: [
              {
                Branches: {
                  Includes: [props.githubBranch],
                },
                FilePaths: filePaths,
              },
            ],
            SourceActionName: codeStarSourceActionName,
          },
          ProviderType: 'CodeStarSourceConnection',
        },
      ]);
    }

    const { synthBuildSpec = DEFAULT_SYNTH_STEP_PARTIAL_BUILD_SPEC } = props;
    const cdkPipeline = new CodePipeline(this, 'CDKCodePipeline', {
      codePipeline: this.pipeline,
      synth: new CodeBuildStep('CdkSynth', {
        installCommands: [
          'node -v',
          'npm install --global corepack@latest',
          'corepack --version',
          'corepack enable',
        ],
        commands: props.cdkSynthCmd,
        input: sourceFile,
        primaryOutputDirectory: props.cdkOut || 'cdk.out',
        partialBuildSpec: BuildSpec.fromObject(synthBuildSpec),
      }),
      selfMutation: true,
      codeBuildDefaults: {
        buildEnvironment: {
          computeType: ComputeType.LARGE,
          buildImage: LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
          environmentVariables: {
            NODE_OPTIONS: {
              value: '--max-old-space-size=8192',
            },
          },
        },
      },
    });

    const defaultStageEnv: StageEnvProps = {
      gamma: GAMMA_ENVIRONMENT,
    };
    const stageEnv = props.stageEnv || defaultStageEnv;

    // After assets are published, they can be removed from the assembly directory
    // to help prevent hitting the CodePipeline artifact limit.
    // https://github.com/aws/aws-cdk/issues/9917
    let stripAssetsFromAssembly: CodeBuildStep | undefined;
    if (props.stripAssemblyAssets) {
      stripAssetsFromAssembly = new CodeBuildStep('StripAssetsFromAssembly', {
        input: cdkPipeline.cloudAssemblyFileSet,
        commands: [
          'S3_PATH=${CODEBUILD_SOURCE_VERSION#"arn:aws:s3:::"}',
          'ZIP_ARCHIVE=$(basename $S3_PATH)',
          'echo $S3_PATH',
          'echo $ZIP_ARCHIVE',
          'ls',
          'rm -rfv asset.*',
          'zip -r -q -A $ZIP_ARCHIVE *',
          'ls',
          'aws s3 cp $ZIP_ARCHIVE s3://$S3_PATH',
        ],
      });

      cdkPipeline.addWave('BeforeStageDeployment', {
        pre: [stripAssetsFromAssembly],
      });
    }

    cdkPipeline.addStage(
      new DeploymentStage(
        this,
        'OrcaBusGamma',
        stageEnv.gamma,
        props.stackName,
        props.stack,
        props.githubRepo,
        props.stackConfig.gamma,
        props.githubBranch
      )
    );

    cdkPipeline.buildPipeline();

    if (stripAssetsFromAssembly) {
      cdkPipeline.pipeline.artifactBucket.grantReadWrite(stripAssetsFromAssembly.project);
    }

    if (props.enableSlackNotification ?? true) {
      const alertsBuildSlackConfigArn = StringParameter.valueForStringParameter(
        this,
        '/chatbot_arn/slack/alerts-build'
      );
      const target = SlackChannelConfiguration.fromSlackChannelConfigurationArn(
        this,
        'SlackChannelConfiguration',
        alertsBuildSlackConfigArn
      );

      this.pipeline.notifyOn('PipelineSlackNotification', target, {
        events: props.notificationEvents || [PipelineNotificationEvents.PIPELINE_EXECUTION_FAILED],
        detailType: DetailType.FULL,
        notificationRuleName: `OrcaBus-${props.pipelineName}`,
      });
    }
  }
}

class DeploymentStage extends Stage {
  constructor(
    scope: Construct,
    environmentName: string,
    env: Environment,
    stackName: string,
    stackClass: new (scope: Construct, id: string, props?: StackProps) => Stack,
    githubRepo: string,
    appStackProps?: Partial<StackProps>,
    githubBranch?: string
  ) {
    super(scope, environmentName, { env: env });

    let source = `https://github.com/OrcaBus/${githubRepo}`;
    if (githubBranch !== undefined && githubBranch !== 'main') {
      source = `${source}/tree/${githubBranch}`;
    }

    new stackClass(this, stackName, {
      env: env,
      tags: {
        'umccr-org:Product': 'OrcaBus',
        'umccr-org:Creator': 'CDK',
        'umccr-org:Service': stackName,
        'umccr-org:Source': source,
      },
      ...appStackProps,
    });
  }
}
