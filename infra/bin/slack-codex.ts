#!/usr/bin/env node
import { App, CfnOutput } from "aws-cdk-lib";

import { GithubActionsDeploy } from "../lib/github-actions-deploy";
import { SlackCodexStack } from "../lib/slack-codex-stack";

const app = new App();
const region =
  app.node.tryGetContext("bedrockRegion") ??
  process.env.CDK_DEFAULT_REGION ??
  "us-east-1";
if (region !== "us-east-1") {
  throw new Error(
    "SlackCodex must deploy in us-east-1 because AgentCore Web Search is only available there.",
  );
}
const githubRepository = app.node.tryGetContext("githubRepository");
const githubOidcSubject = app.node.tryGetContext("githubOidcSubject");

if (
  typeof githubRepository !== "string" ||
  !/^[^/\s]+\/[^/\s]+$/.test(githubRepository) ||
  githubRepository === "OWNER/REPOSITORY"
) {
  throw new Error(
    "Pass -c githubRepository=OWNER/REPOSITORY with the GitHub App repository",
  );
}
if (
  typeof githubOidcSubject !== "string" ||
  !githubOidcSubject.startsWith("repo:") ||
  !githubOidcSubject.endsWith(":ref:refs/heads/main")
) {
  throw new Error(
    "Pass the exact main-branch subject with -c githubOidcSubject=SUBJECT",
  );
}

const stack = new SlackCodexStack(app, "SlackCodex", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region,
  },
  bedrockRegion: region,
  modelId:
    app.node.tryGetContext("bedrockModelId") ?? "openai.gpt-5.6-terra",
  githubRepository,
});

const githubActions = new GithubActionsDeploy(
  stack,
  "GithubActionsDeploy",
  { githubOidcSubject },
);
new CfnOutput(stack, "GithubActionsDeployRoleArn", {
  value: githubActions.role.roleArn,
});
