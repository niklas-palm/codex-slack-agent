import { Stack } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { describe, expect, it } from "vitest";

import { GithubActionsDeploy } from "../lib/github-actions-deploy";

function synthesizeTemplate(): Template {
  const stack = new Stack(undefined, "TestStack", {
    env: { account: "123456789012", region: "us-east-1" },
  });
  new GithubActionsDeploy(stack, "GithubActionsDeploy", {
    githubOidcSubject:
      "repo:owner@123/repository@456:ref:refs/heads/main",
  });
  return Template.fromStack(stack);
}

describe("GithubActionsDeploy", () => {
  it("trusts only main in the configured repository", () => {
    const template = synthesizeTemplate();
    template.hasResourceProperties("AWS::IAM::Role", {
      RoleName: "TestStack-github-actions-deploy",
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "sts:AssumeRoleWithWebIdentity",
            Condition: {
              StringEquals: {
                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                "token.actions.githubusercontent.com:sub":
                  "repo:owner@123/repository@456:ref:refs/heads/main",
              },
            },
            Principal: {
              Federated: Match.anyValue(),
            },
          }),
        ]),
      },
    });
    expect(JSON.stringify(template.toJSON())).toContain(
      "oidc-provider/token.actions.githubusercontent.com",
    );
  });

  it("can assume only the regional CDK bootstrap roles", () => {
    const template = synthesizeTemplate();
    const json = JSON.stringify(template.toJSON());

    for (const role of [
      "deploy-role",
      "file-publishing-role",
      "image-publishing-role",
      "lookup-role",
    ]) {
      expect(json).toContain(
        `cdk-hnb659fds-${role}-123456789012-us-east-1`,
      );
    }
    expect(json).not.toContain("AdministratorAccess");
  });

  it("imports rather than creates the shared OIDC provider", () => {
    synthesizeTemplate().resourceCountIs("AWS::IAM::OIDCProvider", 0);
  });
});
