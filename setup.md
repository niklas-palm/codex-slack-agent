# Setup

This runbook creates the Slack and GitHub identities, deploys the AWS
infrastructure, connects Slack to the public endpoint, and verifies the agent.

The defaults are `us-east-1` and `openai.gpt-5.6-terra`.

Complete the sections in order. In particular, create the Slack app without
Event Subscriptions, deploy the stack, populate its secrets, and only then
enable Event Subscriptions.

## 1. Prerequisites

Install:

- AWS CLI v2
- Node.js 24 and npm
- Docker Desktop with ARM64 build support
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- GitHub CLI (`gh`), authenticated to the repository owner
- `curl` and `jq`

Create the target GitHub repository before continuing. The first deployment
reads that repository's exact OIDC subject from GitHub, and the GitHub App in
step 2 must be installed on it. CDK does not create the repository.

The target account must be able to use `openai.gpt-5.6-terra` through Bedrock
in `us-east-1`.

The AWS principal used for deployment needs permission to bootstrap and deploy
CDK resources including IAM, Lambda, HTTP API, ECR, Secrets Manager,
CloudFormation, CloudWatch Logs, and Bedrock AgentCore.

Configure a profile and verify the account:

```bash
export AWS_PROFILE=slack-agent
export AWS_REGION=us-east-1
aws sts get-caller-identity
```

The stack imports the account's shared GitHub OIDC provider. Confirm it exists:

```bash
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
GITHUB_OIDC_PROVIDER_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"

aws iam get-open-id-connect-provider \
  --open-id-connect-provider-arn "$GITHUB_OIDC_PROVIDER_ARN"
```

If it is absent, create one shared provider:

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com
```

The stack deliberately does not own this account-wide provider because other
repositories may use it.

## 2. Create the GitHub App

The GitHub App gives pull requests their own bot identity and issues
short-lived installation tokens. No GitHub webhook is required; Slack is the
only trigger.

1. Open your personal or organization **Settings > Developer settings >
   GitHub Apps > New GitHub App**.
2. Choose a name, such as `Codex Slack Agent`.
3. Set the homepage URL to the target repository.
4. Clear **Active** under Webhook.
5. Set repository permissions:
   - **Actions:** Read-only
   - **Checks:** Read-only
   - **Contents:** Read and write
   - **Pull requests:** Read and write
   - **Metadata:** Read-only, added automatically
6. Limit installation to your account or organization.
7. Create the app and record its numeric **App ID**.
8. Generate and download a private key.
9. Select **Install App** and install it on the one target repository.
10. Record the numeric installation ID from the final segment of the
    installation URL, such as
    `https://github.com/settings/installations/INSTALLATION_ID`.

The App does not need Administration, Issues, workflow write, or webhook
permissions. Actions and Checks are read-only so the agent can inspect CI
status and failed workflow logs. Its Client ID and Client Secret are not used.

## 3. Create the Slack app

You can create the app now, but leave Event Subscriptions disabled until CDK
has produced the request URL.

1. Open [Slack API apps](https://api.slack.com/apps).
2. Select **Create New App > From an app manifest**.
3. Choose the workspace and paste `slack-app-manifest.yaml`.
4. Create the app.
5. Under **OAuth & Permissions**, install it to the workspace.
6. Record the **Bot User OAuth Token** (`xoxb-...`).
7. Under **Basic Information > App Credentials**, record the **Signing Secret**.

The manifest intentionally omits `settings.event_subscriptions`. Slack rejects
a manifest that declares `app_mention` without either a Request URL or Socket
Mode, and the Request URL does not exist until after deployment.

The manifest grants:

- `app_mentions:read`
- `channels:history`
- `groups:history`
- `chat:write`
- `reactions:write`
- `files:read`
- `files:write`

## 4. Install and test locally

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

These tests do not call Slack, GitHub, Bedrock, or AgentCore.

## 5. Bootstrap and deploy

From `infra/`, bootstrap the account once:

```bash
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
npx cdk bootstrap "aws://${ACCOUNT_ID}/${AWS_REGION}"
```

Deploy with the repository the App was installed on:

```bash
export GITHUB_REPOSITORY=OWNER/REPOSITORY
export GITHUB_OIDC_SUBJECT="$(
  gh api "repos/${GITHUB_REPOSITORY}/actions/oidc/customization/sub" \
    --jq '.sub_claim_prefix + ":ref:refs/heads/main"'
)"

npm run deploy -- \
  --require-approval never \
  -c githubRepository="$GITHUB_REPOSITORY" \
  -c githubOidcSubject="$GITHUB_OIDC_SUBJECT" \
  -c bedrockRegion=us-east-1 \
  -c bedrockModelId=openai.gpt-5.6-terra
```

Read the subject from GitHub rather than constructing it. Depending on the
repository, GitHub may include immutable owner and repository IDs in the
prefix.

Record these outputs:

- `AgentRuntimeArn`
- `SlackEventsUrl`
- `SlackSigningSecretArn`
- `SlackBotTokenArn`
- `GithubAppCredentialsArn`
- `GithubActionsDeployRoleArn`

CDK creates generated placeholder secret values so credentials never appear in
source or CloudFormation parameters. Replace all three placeholders before the
live smoke test in step 7.

You can load the outputs into shell variables:

```bash
stack_output() {
  aws cloudformation describe-stacks \
    --stack-name SlackCodex \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" \
    --output text
}

export AGENT_RUNTIME_ARN="$(stack_output AgentRuntimeArn)"
export SLACK_EVENTS_URL="$(stack_output SlackEventsUrl)"
export SLACK_SIGNING_SECRET_ARN="$(stack_output SlackSigningSecretArn)"
export SLACK_BOT_TOKEN_ARN="$(stack_output SlackBotTokenArn)"
export GITHUB_APP_CREDENTIALS_ARN="$(stack_output GithubAppCredentialsArn)"
export GITHUB_ACTIONS_DEPLOY_ROLE_ARN="$(stack_output GithubActionsDeployRoleArn)"
```

## 6. Populate Secrets Manager

Set the two Slack secrets:

```bash
printf "Slack signing secret: "
read -r -s SLACK_SIGNING_SECRET_VALUE
printf "\n"
aws secretsmanager put-secret-value \
  --secret-id "$SLACK_SIGNING_SECRET_ARN" \
  --secret-string "$SLACK_SIGNING_SECRET_VALUE"
unset SLACK_SIGNING_SECRET_VALUE

printf "Slack bot token: "
read -r -s SLACK_BOT_TOKEN_VALUE
printf "\n"
aws secretsmanager put-secret-value \
  --secret-id "$SLACK_BOT_TOKEN_ARN" \
  --secret-string "$SLACK_BOT_TOKEN_VALUE"
unset SLACK_BOT_TOKEN_VALUE
```

Use the actual ARN or the exported shell variable, not the literal placeholder
name. The silent prompts keep secret values out of shell history.

Build the GitHub App JSON without printing the private key:

```bash
umask 077
jq -n \
  --arg app_id 'YOUR_APP_ID' \
  --arg installation_id 'YOUR_INSTALLATION_ID' \
  --rawfile private_key '/path/to/github-app.private-key.pem' \
  '{app_id:$app_id, installation_id:$installation_id, private_key:$private_key}' \
  > /tmp/slack-codex-github-app.json

aws secretsmanager put-secret-value \
  --secret-id "$GITHUB_APP_CREDENTIALS_ARN" \
  --secret-string file:///tmp/slack-codex-github-app.json

rm /tmp/slack-codex-github-app.json
```

Secrets are loaded when a new AgentCore microVM starts. Use a new test session
name after changing credentials. Existing sessions retain the values they
loaded at startup.

Lambda reads the current Slack secret values on every event, so correcting or
rotating the signing secret takes effect without redeployment.

## 7. Verify AgentCore before Slack

Run the automated live smoke test with stubbed Slack:

```bash
cd infra
AWS_PROFILE="$AWS_PROFILE" \
AWS_REGION="$AWS_REGION" \
AGENT_RUNTIME_ARN="$AGENT_RUNTIME_ARN" \
npm run test:e2e
```

Expected result:

- `status` is `completed`
- `replied` is `true`
- `thread_status` is `done`
- reactions move from eyes to yellow to green
- exactly one reply appears in `slack.posts`
- the command prints `AgentCore smoke checks passed.`

For an interactive follow-up test, call `npm run invoke:test` twice with the
same `--session` value. The second result should include the earlier messages
in `slack.thread`, and files created by the first turn should remain available.

## 8. Connect Slack

Slack signs its URL-verification request. Complete step 6 first; if the
deployed signing-secret resource still contains its generated placeholder,
Lambda returns HTTP 401 and Slack reports that the URL did not return the
challenge.

1. Open the Slack app's **Event Subscriptions** page.
2. Enable events.
3. Set **Request URL** to the `SlackEventsUrl` output or
   `$SLACK_EVENTS_URL`.
4. Wait for Slack's URL verification to succeed.
5. Under **Subscribe to bot events**, add `app_mention`.
6. Reinstall the app if Slack requests it.
7. Invite `@Codex` to a test channel.

Leave **Delayed Events** off for this sample. Lambda deliberately ignores Slack
retry deliveries, while AgentCore independently deduplicates event IDs within
the session.

Test a read-only turn:

```text
@Codex read this thread and summarize the request
```

Then test repository access:

```text
@Codex inspect OWNER/REPOSITORY and open a PR that fixes a small documentation typo
```

The second request should create a `codex/*` branch and a ready PR authored by
the GitHub App. Review and merge it manually.

## 9. Logs and diagnostics

Find and tail AgentCore logs:

```bash
aws logs describe-log-groups \
  --log-group-name-prefix /aws/bedrock-agentcore/runtimes/ \
  --query 'logGroups[].logGroupName'

aws logs tail AGENTCORE_LOG_GROUP --since 30m --follow
```

Find the ingress Lambda log group:

```bash
aws cloudformation describe-stack-resources \
  --stack-name SlackCodex \
  --query "StackResources[?ResourceType=='AWS::Logs::LogGroup'].PhysicalResourceId"
```

Common failures:

| Symptom | Check |
|---|---|
| Slack reports an HTTP error or missing challenge | Populate the signing-secret resource in step 6, then retry |
| Slack reports `invalid_auth` | Populate the bot-token resource with the current `xoxb-...` token and reinstall the app if its scopes changed |
| No `eyes` reaction | Bot token, `reactions:write`, and channel membership |
| Eyes but no reply | AgentCore logs and Bedrock model access |
| Thread/file API errors | History/file scopes and channel membership |
| Git clone returns 401/403 | App installation, repository selection, and Contents permission |
| PR creation returns 403 | Pull requests read/write permission |
| Follow-up lost context | Same Slack thread and an unexpired AgentCore session |

To confirm the endpoint itself is reachable, an unsigned request should return
HTTP 401:

```bash
curl -i -X POST "$SLACK_EVENTS_URL" -d '{}'
```

That 401 is expected. A successful Slack verification requires Slack's signed
request and the matching Signing Secret.

## 10. Enable GitHub Actions deployment

The stack creates a deploy role whose OIDC trust is restricted to this
repository's `main` branch. The role can only assume the four regional CDK
bootstrap roles; it does not receive `AdministratorAccess` directly.

Set the role ARN and exact GitHub subject as repository Actions variables:

```bash
gh variable set AWS_DEPLOY_ROLE_ARN \
  --repo OWNER/REPOSITORY \
  --body "$GITHUB_ACTIONS_DEPLOY_ROLE_ARN"

gh variable set AWS_OIDC_SUBJECT \
  --repo OWNER/REPOSITORY \
  --body "$GITHUB_OIDC_SUBJECT"
```

The committed `.github/workflows/deploy.yml` workflow runs all runtime and
infrastructure tests on pull requests. After a merge to `main`, it reruns the
tests, configures short-lived AWS credentials through OIDC, builds the ARM64
image, and deploys CDK. Establish the `Test` check with one successful run:

```bash
gh workflow run deploy.yml --repo OWNER/REPOSITORY --ref main
gh run watch --repo OWNER/REPOSITORY
```

Then protect `main`:

```bash
gh api --method PUT \
  repos/OWNER/REPOSITORY/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["Test"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 0,
    "require_last_push_approval": false
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false,
  "required_conversation_resolution": true,
  "lock_branch": false,
  "allow_fork_syncing": true
}
JSON
```

This requires every change to arrive through a pull request and pass the
current `Test` check. It requires no approval so a single maintainer can merge
their own demo changes. Set `required_approving_review_count` to `1` when
another reviewer is available. GitHub requires a public repository or a plan
that supports branch protection for private repositories.

No AWS access keys are stored in GitHub. Only the deploy job receives
`id-token: write`; all third-party actions are pinned to commit SHAs.

## 11. Rotate or remove

Rotate Slack or GitHub values with `put-secret-value`, then use a new AgentCore
session. Routine CDK deployments keep the HTTP API URL stable. If the stack is
deleted and recreated, update Slack's Request URL.

Destroy the stack:

```bash
cd infra
npx cdk destroy --force
```

The explicitly created Lambda log group is retained for diagnostics. Delete it
manually after reviewing the final logs if you want to remove every
application-owned resource.
