import * as path from "node:path";

import {
  CfnOutput,
  Duration,
  Stack,
  type StackProps,
} from "aws-cdk-lib";
import { CfnRuntime } from "aws-cdk-lib/aws-bedrockagentcore";
import {
  HttpApi,
  HttpMethod,
} from "aws-cdk-lib/aws-apigatewayv2";
import { HttpLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import {
  Effect,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from "aws-cdk-lib/aws-iam";
import {
  Architecture,
  Runtime,
} from "aws-cdk-lib/aws-lambda";
import { NodejsFunction } from "aws-cdk-lib/aws-lambda-nodejs";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { Secret } from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

export interface SlackCodexStackProps extends StackProps {
  bedrockRegion: string;
  modelId: string;
  githubRepository: string;
}

export class SlackCodexStack extends Stack {
  constructor(scope: Construct, id: string, props: SlackCodexStackProps) {
    super(scope, id, props);

    const repositoryRoot = path.join(__dirname, "..", "..");
    const runtimeRoot = path.join(repositoryRoot, "runtime");

    const signingSecret = new Secret(this, "SlackSigningSecret", {
      description: "Slack app signing secret; replace the generated placeholder after deploy",
    });
    const slackBotToken = new Secret(this, "SlackBotToken", {
      description: "Slack bot token; replace the generated placeholder after deploy",
    });
    const githubAppCredentials = new Secret(this, "GithubAppCredentials", {
      description: "GitHub App ID, installation ID, and private key as JSON",
      generateSecretString: {
        secretStringTemplate: JSON.stringify({
          app_id: "replace-me",
          installation_id: "replace-me",
        }),
        generateStringKey: "private_key",
        excludePunctuation: true,
      },
    });

    const image = new DockerImageAsset(this, "RuntimeImage", {
      directory: runtimeRoot,
      platform: Platform.LINUX_ARM64,
    });

    const runtimeRole = new Role(this, "RuntimeRole", {
      assumedBy: new ServicePrincipal("bedrock-agentcore.amazonaws.com"),
      description: "Execution role for the Slack Codex AgentCore runtime",
    });
    image.repository.grantPull(runtimeRole);
    runtimeRole.addToPolicy(
      new PolicyStatement({
        actions: ["ecr:GetAuthorizationToken"],
        resources: ["*"],
      }),
    );
    runtimeRole.addToPolicy(
      new PolicyStatement({
        actions: ["bedrock-mantle:CreateInference"],
        resources: ["*"],
      }),
    );
    runtimeRole.addToPolicy(
      new PolicyStatement({
        actions: [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
          "logs:PutLogEvents",
        ],
        resources: ["*"],
      }),
    );
    slackBotToken.grantRead(runtimeRole);
    githubAppCredentials.grantRead(runtimeRole);

    const agentRuntime = new CfnRuntime(this, "AgentRuntime", {
      agentRuntimeName: "slack_codex_agent",
      description: "Slack-triggered OpenAI Agents SDK code agent",
      roleArn: runtimeRole.roleArn,
      agentRuntimeArtifact: {
        containerConfiguration: {
          containerUri: image.imageUri,
        },
      },
      networkConfiguration: {
        networkMode: "PUBLIC",
      },
      protocolConfiguration: "HTTP",
      environmentVariables: {
        AWS_REGION: this.region,
        BEDROCK_REGION: props.bedrockRegion,
        BEDROCK_MODEL_ID: props.modelId,
        SLACK_BOT_TOKEN_SECRET_ARN: slackBotToken.secretArn,
        GITHUB_APP_CREDENTIALS_SECRET_ARN: githubAppCredentials.secretArn,
        GH_REPO: props.githubRepository,
        WORKSPACE_DIR: "/workspace",
      },
      lifecycleConfiguration: {
        idleRuntimeSessionTimeout: 28_800,
        maxLifetime: 28_800,
      },
    });

    const defaultPolicy = runtimeRole.node.tryFindChild("DefaultPolicy");
    if (defaultPolicy) {
      agentRuntime.node.addDependency(defaultPolicy);
    }

    const lambdaLogGroup = new LogGroup(this, "SlackEventsLogGroup", {
      retention: RetentionDays.ONE_MONTH,
    });
    const slackEvents = new NodejsFunction(this, "SlackEvents", {
      entry: path.join(repositoryRoot, "infra", "functions", "slack-events.ts"),
      handler: "handler",
      runtime: Runtime.NODEJS_24_X,
      architecture: Architecture.ARM_64,
      memorySize: 256,
      timeout: Duration.seconds(15),
      logGroup: lambdaLogGroup,
      bundling: {
        externalModules: [],
        minify: true,
        sourceMap: true,
      },
      environment: {
        AGENT_RUNTIME_ARN: agentRuntime.attrAgentRuntimeArn,
        SLACK_SIGNING_SECRET_ARN: signingSecret.secretArn,
        SLACK_BOT_TOKEN_ARN: slackBotToken.secretArn,
      },
    });
    signingSecret.grantRead(slackEvents);
    slackBotToken.grantRead(slackEvents);
    slackEvents.addToRolePolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["bedrock-agentcore:InvokeAgentRuntime"],
        resources: [
          agentRuntime.attrAgentRuntimeArn,
          `${agentRuntime.attrAgentRuntimeArn}/runtime-endpoint/*`,
        ],
      }),
    );

    const api = new HttpApi(this, "SlackApi", {
      apiName: "slack-codex-events",
      description: "Public Slack events endpoint authenticated by Slack HMAC",
    });
    api.addRoutes({
      path: "/slack/events",
      methods: [HttpMethod.POST],
      integration: new HttpLambdaIntegration(
        "SlackEventsIntegration",
        slackEvents,
      ),
    });

    new CfnOutput(this, "SlackEventsUrl", {
      value: `${api.apiEndpoint}/slack/events`,
    });
    new CfnOutput(this, "AgentRuntimeArn", {
      value: agentRuntime.attrAgentRuntimeArn,
    });
    new CfnOutput(this, "SlackSigningSecretArn", {
      value: signingSecret.secretArn,
    });
    new CfnOutput(this, "SlackBotTokenArn", {
      value: slackBotToken.secretArn,
    });
    new CfnOutput(this, "GithubAppCredentialsArn", {
      value: githubAppCredentials.secretArn,
    });
  }
}
