import { App } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { beforeAll, describe, expect, it } from "vitest";

import { SlackCodexStack } from "../lib/slack-codex-stack";

function synthesizeTemplate(): Template {
  const app = new App();
  const stack = new SlackCodexStack(app, "TestStack", {
    env: { account: "123456789012", region: "us-east-1" },
    bedrockRegion: "us-east-1",
    modelId: "openai.gpt-5.6-terra",
    githubRepository: "owner/repository",
  });
  return Template.fromStack(stack);
}

describe("SlackCodexStack", () => {
  let stackTemplate: Template;

  beforeAll(() => {
    stackTemplate = synthesizeTemplate();
  }, 30_000);

  it("creates the eight-hour public AgentCore runtime", () => {
    stackTemplate.hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      NetworkConfiguration: { NetworkMode: "PUBLIC" },
      ProtocolConfiguration: "HTTP",
      LifecycleConfiguration: {
        IdleRuntimeSessionTimeout: 28800,
        MaxLifetime: 28800,
      },
      EnvironmentVariables: Match.objectLike({
        BEDROCK_REGION: "us-east-1",
        BEDROCK_MODEL_ID: "openai.gpt-5.6-terra",
        GITHUB_APP_CREDENTIALS_SECRET_ARN: Match.anyValue(),
        GH_REPO: "owner/repository",
      }),
    });
  });

  it("uses a public route with no API Gateway authorizer", () => {
    stackTemplate.hasResourceProperties("AWS::ApiGatewayV2::Route", {
      RouteKey: "POST /slack/events",
      AuthorizationType: "NONE",
    });
  });

  it("creates three externally populated secrets", () => {
    stackTemplate.resourceCountIs("AWS::SecretsManager::Secret", 3);
    stackTemplate.hasResourceProperties("AWS::SecretsManager::Secret", {
      GenerateSecretString: {
        SecretStringTemplate:
          '{"app_id":"replace-me","installation_id":"replace-me"}',
        GenerateStringKey: "private_key",
      },
    });
  });

  it("uses an ARM64 Node.js 24 ingress Lambda", () => {
    stackTemplate.hasResourceProperties("AWS::Lambda::Function", {
      Architectures: ["arm64"],
      Runtime: "nodejs24.x",
    });
  });

  it("grants only the expected invocation and inference actions", () => {
    const json = JSON.stringify(stackTemplate.toJSON());
    expect(json).toContain("bedrock-agentcore:InvokeAgentRuntime");
    expect(json).toContain("/runtime-endpoint/*");
    expect(json).toContain("bedrock-mantle:CreateInference");
    expect(json).not.toContain("events:PutEvents");
    expect(json).not.toContain("sqs:");
    expect(json).not.toContain("dynamodb:");
  });
});
