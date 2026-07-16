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
        WEB_SEARCH_GATEWAY_URL: Match.anyValue(),
        WEB_SEARCH_GATEWAY_REGION: "us-east-1",
      }),
    });
  });

  it("creates an IAM-protected Web Search MCP Gateway", () => {
    stackTemplate.hasResourceProperties("AWS::BedrockAgentCore::Gateway", {
      Name: "slack-codex-web-search",
      AuthorizerType: "AWS_IAM",
      ProtocolType: "MCP",
      ProtocolConfiguration: {
        Mcp: {
          SupportedVersions: ["2025-03-26"],
        },
      },
      RoleArn: Match.anyValue(),
    });
    stackTemplate.hasResourceProperties("AWS::BedrockAgentCore::GatewayTarget", {
      Name: "web-search",
      GatewayIdentifier: Match.anyValue(),
      TargetConfiguration: {
        Mcp: {
          Connector: {
            Source: {
              ConnectorId: "web-search",
            },
            Configurations: [
              {
                Name: "WebSearch",
                ParameterValues: {},
              },
            ],
          },
        },
      },
      CredentialProviderConfigurations: [
        {
          CredentialProviderType: "GATEWAY_IAM_ROLE",
        },
      ],
    });
  });

  it("grants only the Web Search Gateway roles their required permissions", () => {
    stackTemplate.hasResourceProperties(
      "AWS::IAM::Policy",
      Match.objectLike({
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "bedrock-agentcore:InvokeWebSearch",
              Resource: Match.anyValue(),
            }),
          ]),
        }),
      }),
    );
    stackTemplate.hasResourceProperties(
      "AWS::IAM::Policy",
      Match.objectLike({
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "bedrock-agentcore:InvokeGateway",
              Resource: Match.anyValue(),
            }),
          ]),
        }),
      }),
    );
    stackTemplate.hasResourceProperties(
      "AWS::IAM::Role",
      Match.objectLike({
        AssumeRolePolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: "sts:AssumeRole",
              Principal: {
                Service: "bedrock-agentcore.amazonaws.com",
              },
              Condition: Match.objectLike({
                StringEquals: {
                  "aws:SourceAccount": "123456789012",
                },
                ArnLike: Match.anyValue(),
              }),
            }),
          ]),
        }),
      }),
    );
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

  it("attaches AWS managed ReadOnlyAccess to the AgentCore runtime role", () => {
    stackTemplate.hasResourceProperties("AWS::IAM::Role", {
      ManagedPolicyArns: [
        { "Fn::Join": ["", ["arn:", { Ref: "AWS::Partition" }, ":iam::aws:policy/ReadOnlyAccess"]] },
      ],
    });
  });

  it("grants the expected runtime invocation and inference actions", () => {
    const json = JSON.stringify(stackTemplate.toJSON());
    expect(json).toContain("bedrock-agentcore:InvokeAgentRuntime");
    expect(json).toContain("bedrock-agentcore:InvokeGateway");
    expect(json).toContain("bedrock-agentcore:InvokeWebSearch");
    expect(json).toContain("web-search.v1");
    expect(json).toContain("/runtime-endpoint/*");
    expect(json).toContain("bedrock-mantle:CreateInference");
    expect(json).not.toContain("events:PutEvents");
    expect(json).not.toContain("sqs:");
    expect(json).not.toContain("dynamodb:");
  });

  it("rejects regions that do not support managed Web Search", () => {
    expect(
      () =>
        new SlackCodexStack(new App(), "WrongRegion", {
          env: { account: "123456789012", region: "us-west-2" },
          bedrockRegion: "us-west-2",
          modelId: "openai.gpt-5.6-terra",
          githubRepository: "owner/repository",
        }),
    ).toThrow("us-east-1");
  });
});
