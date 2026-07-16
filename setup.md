# Setup

This runbook deploys the Slack Codex Agent to AWS, connects it to Slack, and verifies the installation. Complete the sections in order.

The default model is `openai.gpt-5.6-luna`; the stack is fixed to `us-east-1` because the managed Web Search integration is available there.

## 1. Prerequisites

Install and authenticate:

- AWS CLI v2 with permission to bootstrap and deploy CDK resources
- Node.js 24 and npm
- Docker Desktop with ARM64 build support
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- GitHub CLI (`gh`)
- `curl` and `jq`

Create the target GitHub repository and push this project to its `main` branch. The GitHub App and OIDC trust are scoped to this repository. Ensure the account can use the configured Bedrock model in `us-east-1`.

```bash
export AWS_PROFILE=slack-agent
export AWS_REGION=us-east-1
aws sts get-caller-identity
```

The account must have a shared GitHub Actions OIDC provider. Check it with:

```bash
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
aws iam get-open-id-connect-provider \
  --open-id-connect-provider-arn "arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
```

If it is missing, create it once:

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com
```

## 2. Create the GitHub App

Create a GitHub App under **Settings → Developer settings → GitHub Apps**:

1. Set the homepage to the target repository and disable the webhook.
2. Grant repository permissions: **Contents: Read and write**, **Pull requests: Read and write**, **Actions: Read-only**, **Checks: Read-only**. Metadata is automatic.
3. Restrict installation to the owner or organization, create the App, and record its numeric App ID.
4. Generate a private key and install the App on the target repository.
5. Record the numeric installation ID from the installation URL.

No webhook, Client Secret, Administration, Issues, or workflow-write permission is needed.

## 3. Create the Slack app

1. In [Slack API apps](https://api.slack.com/apps), choose **Create New App → From an app manifest**.
2. Select the workspace and paste [`slack-app-manifest.yaml`](slack-app-manifest.yaml).
3. Install the app under **OAuth & Permissions** and record the bot token (`xoxb-...`).
4. Record the **Signing Secret** under **Basic Information → App Credentials**.

Leave **Event Subscriptions** disabled. The Request URL does not exist until deployment.

## 4. Run offline checks

```bash
cd runtime
uv sync --frozen
uv run ruff check .
uv run pytest

cd ../infra
npm ci
npm run build
npm test
```

These checks do not call Slack, GitHub, Bedrock, or AgentCore.

## 5. Bootstrap and deploy

From `infra/`, bootstrap the account once:

```bash
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
npx cdk bootstrap "aws://${ACCOUNT_ID}/${AWS_REGION}"
```

Set the repository and the standard main-branch OIDC subject:

```bash
export GITHUB_REPOSITORY=OWNER/REPOSITORY
export GITHUB_OIDC_SUBJECT="repo:${GITHUB_REPOSITORY}:ref:refs/heads/main"
```

Deploy:

```bash
npm run deploy -- \
  --require-approval never \
  -c githubRepository="$GITHUB_REPOSITORY" \
  -c githubOidcSubject="$GITHUB_OIDC_SUBJECT" \
  -c bedrockRegion=us-east-1 \
  -c bedrockModelId=openai.gpt-5.6-luna
```

Export the outputs needed below:

```bash
stack_output() {
  aws cloudformation describe-stacks --stack-name SlackCodex \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text
}

export AGENT_RUNTIME_ARN="$(stack_output AgentRuntimeArn)"
export SLACK_EVENTS_URL="$(stack_output SlackEventsUrl)"
export SLACK_SIGNING_SECRET_ARN="$(stack_output SlackSigningSecretArn)"
export SLACK_BOT_TOKEN_ARN="$(stack_output SlackBotTokenArn)"
export GITHUB_APP_CREDENTIALS_ARN="$(stack_output GithubAppCredentialsArn)"
export GITHUB_ACTIONS_DEPLOY_ROLE_ARN="$(stack_output GithubActionsDeployRoleArn)"
```

## 6. Populate secrets

CDK creates placeholders so credentials are never passed through source or CloudFormation. Replace all three before testing:

```bash
printf "Signing secret: "; read -r -s SLACK_SIGNING_SECRET_VALUE; printf "\n"
aws secretsmanager put-secret-value --secret-id "$SLACK_SIGNING_SECRET_ARN" \
  --secret-string "$SLACK_SIGNING_SECRET_VALUE"
unset SLACK_SIGNING_SECRET_VALUE

printf "Bot token: "; read -r -s SLACK_BOT_TOKEN_VALUE; printf "\n"
aws secretsmanager put-secret-value --secret-id "$SLACK_BOT_TOKEN_ARN" \
  --secret-string "$SLACK_BOT_TOKEN_VALUE"
unset SLACK_BOT_TOKEN_VALUE
```

Create the GitHub App JSON without printing the private key:

```bash
umask 077
jq -n --arg app_id YOUR_APP_ID --arg installation_id YOUR_INSTALLATION_ID \
  --rawfile private_key /path/to/github-app.private-key.pem \
  '{app_id:$app_id, installation_id:$installation_id, private_key:$private_key}' \
  > /tmp/slack-codex-github-app.json
aws secretsmanager put-secret-value --secret-id "$GITHUB_APP_CREDENTIALS_ARN" \
  --secret-string file:///tmp/slack-codex-github-app.json
rm /tmp/slack-codex-github-app.json
```

Secrets load when a new AgentCore microVM starts. Use a new test session after changing GitHub credentials. Lambda reads the Slack signing secret for every event.

## 7. Verify AgentCore

Run the live smoke tests with Slack stubbed:

```bash
cd infra
AGENT_RUNTIME_ARN="$AGENT_RUNTIME_ARN" npm run test:e2e
AGENT_RUNTIME_ARN="$AGENT_RUNTIME_ARN" npm run test:e2e:web
```

The first checks tools, repository authentication, filesystem access, and Slack protocol handling. The second checks managed search and public page fetching. These tests use Bedrock and AgentCore and may incur charges.

## 8. Connect Slack

1. Open the Slack app’s **Event Subscriptions** page and enable events.
2. Set **Request URL** to `$SLACK_EVENTS_URL` and wait for verification.
3. Subscribe to the `app_mention` bot event; reinstall if Slack requests it.
4. Invite `@Codex` to a test channel and send:

   ```text
   @Codex read this thread and summarize the request
   ```

An unsigned request should return HTTP 401; that confirms the public endpoint is rejecting unauthenticated traffic:

```bash
curl -i -X POST "$SLACK_EVENTS_URL" -d '{}'
```

## 9. Enable GitHub Actions deployment

Set the deployment role and exact subject as repository variables:

```bash
gh variable set AWS_DEPLOY_ROLE_ARN --repo "$GITHUB_REPOSITORY" --body "$GITHUB_ACTIONS_DEPLOY_ROLE_ARN"
gh variable set AWS_OIDC_SUBJECT --repo "$GITHUB_REPOSITORY" --body "$GITHUB_OIDC_SUBJECT"
```

The workflow runs tests on pull requests and deploys merges to `main` through OIDC. Run it once to establish the `Test` check:

```bash
gh workflow run deploy.yml --repo "$GITHUB_REPOSITORY" --ref main
gh run watch --repo "$GITHUB_REPOSITORY"
```

Then protect `main` in GitHub and require the `Test` check and pull requests. No AWS access keys are stored in GitHub.

## Troubleshooting

- *Slack URL verification fails:* confirm the signing-secret secret contains the current value, not the generated placeholder.
- *`invalid_auth` or no reaction:* check the bot token, required scopes, channel membership, and reinstall the app after scope changes.
- *No reply:* inspect AgentCore logs and Bedrock model access.
- *GitHub clone or PR fails:* check App installation, repository selection, and Contents/Pull requests permissions.
- *Follow-up loses context:* use the same Slack thread and mention `@Codex`; sessions expire after about eight hours.
- *Web fetch fails:* only public HTTPS server-rendered HTML, XHTML, and plain text are supported; JavaScript-only and private pages are rejected.

AgentCore and `/workspace` state last only while the microVM is alive. To remove the stack:

```bash
cd infra
npx cdk destroy --force
```

The application log group is retained for diagnostics and may need separate deletion.
