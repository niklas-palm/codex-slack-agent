import { createHash, createHmac, timingSafeEqual } from "node:crypto";

import {
  BedrockAgentCoreClient,
  InvokeAgentRuntimeCommand,
} from "@aws-sdk/client-bedrock-agentcore";
import {
  GetSecretValueCommand,
  SecretsManagerClient,
} from "@aws-sdk/client-secrets-manager";
import type {
  APIGatewayProxyEventV2,
  APIGatewayProxyStructuredResultV2,
} from "aws-lambda";

type AwsClient = {
  send(command: unknown): Promise<Record<string, unknown>>;
};

export interface HandlerDependencies {
  agentCore: AwsClient;
  secrets: AwsClient;
  fetch: typeof fetch;
  now: () => number;
}

interface SlackMessageEvent {
  type?: string;
  text?: string;
  user?: string;
  ts?: string;
  thread_ts?: string;
  channel?: string;
  bot_id?: string;
  app_id?: string;
  subtype?: string;
}

interface SlackCallback {
  type?: string;
  event_id?: string;
  team_id?: string;
  challenge?: string;
  event?: SlackMessageEvent;
}

const STATUS_EMOJI = [
  "large_yellow_circle",
  "question",
  "large_green_circle",
  "red_circle",
];

function requiredEnv(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

function header(
  headers: Record<string, string | undefined>,
  name: string,
): string | undefined {
  const wanted = name.toLowerCase();
  const entry = Object.entries(headers).find(
    ([key]) => key.toLowerCase() === wanted,
  );
  return entry?.[1];
}

export function verifySlackSignature(
  signature: string | undefined,
  timestamp: string | undefined,
  rawBody: string,
  signingSecret: string,
  nowMs: number,
): boolean {
  if (!signature || !timestamp) {
    return false;
  }
  const seconds = Number(timestamp);
  if (!Number.isFinite(seconds) || Math.abs(nowMs / 1000 - seconds) > 300) {
    return false;
  }
  const expected =
    "v0=" +
    createHmac("sha256", signingSecret)
      .update(`v0:${timestamp}:${rawBody}`)
      .digest("hex");
  const actualBuffer = Buffer.from(signature);
  const expectedBuffer = Buffer.from(expected);
  return (
    actualBuffer.length === expectedBuffer.length &&
    timingSafeEqual(actualBuffer, expectedBuffer)
  );
}

export function sessionIdFor(
  teamId: string,
  channelId: string,
  threadTs: string,
): string {
  const digest = createHash("sha256")
    .update(`${teamId}\0${channelId}\0${threadTs}`)
    .digest("hex");
  return `slack_${digest}`;
}

export function stripMention(text: string): string {
  return text.replace(/^\s*<@[A-Z0-9]+>\s*/i, "").trim();
}

async function responseText(value: unknown): Promise<string> {
  if (!value) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (value instanceof Uint8Array) {
    return Buffer.from(value).toString("utf8");
  }
  const transform = (value as { transformToString?: () => Promise<string> })
    .transformToString;
  return transform ? transform.call(value) : "";
}

export function createHandler(
  dependencies?: Partial<HandlerDependencies>,
): (
  event: APIGatewayProxyEventV2,
) => Promise<APIGatewayProxyStructuredResultV2> {
  const region = process.env.AWS_REGION ?? "us-east-1";
  const deps: HandlerDependencies = {
    agentCore:
      dependencies?.agentCore ??
      (new BedrockAgentCoreClient({ region }) as unknown as AwsClient),
    secrets:
      dependencies?.secrets ??
      (new SecretsManagerClient({ region }) as unknown as AwsClient),
    fetch: dependencies?.fetch ?? fetch,
    now: dependencies?.now ?? Date.now,
  };

  async function secretValue(secretArn: string): Promise<string> {
    const response = await deps.secrets.send(
      new GetSecretValueCommand({ SecretId: secretArn }),
    );
    const value = response.SecretString;
    if (typeof value !== "string" || !value) {
      throw new Error(`Secret has no SecretString: ${secretArn}`);
    }
    return value;
  }

  async function slackCall(
    token: string,
    method: string,
    body: Record<string, unknown>,
    ignoredErrors: string[] = [],
  ): Promise<Record<string, unknown>> {
    const response = await deps.fetch(`https://slack.com/api/${method}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json; charset=utf-8",
      },
      body: JSON.stringify(body),
    });
    const result = (await response.json()) as {
      ok?: boolean;
      error?: string;
    };
    if (!response.ok || (!result.ok && !ignoredErrors.includes(result.error ?? ""))) {
      throw new Error(`${method}: ${result.error ?? `HTTP ${response.status}`}`);
    }
    return result as Record<string, unknown>;
  }

  async function addReaction(
    token: string,
    channel: string,
    timestamp: string,
    name: string,
  ): Promise<void> {
    await slackCall(
      token,
      "reactions.add",
      { channel, timestamp, name },
      ["already_reacted"],
    );
  }

  async function setFailureStatus(
    token: string,
    channel: string,
    threadTs: string,
    triggerTs: string,
  ): Promise<void> {
    for (const timestamp of [...new Set([threadTs, triggerTs])]) {
      for (const name of STATUS_EMOJI) {
        await slackCall(
          token,
          "reactions.remove",
          { channel, timestamp, name },
          ["no_reaction", "message_not_found"],
        ).catch((error) =>
          console.warn("Failed to remove status reaction", error),
        );
      }
      await addReaction(token, channel, timestamp, "red_circle");
    }
  }

  async function invokeRuntime(
    runtimeArn: string,
    runtimeSessionId: string,
    payload: Record<string, unknown>,
  ): Promise<void> {
    const response = await deps.agentCore.send(
      new InvokeAgentRuntimeCommand({
        agentRuntimeArn: runtimeArn,
        runtimeSessionId,
        payload: Buffer.from(JSON.stringify(payload)),
        contentType: "application/json",
        accept: "application/json",
      }),
    );
    const statusCode =
      typeof response.statusCode === "number" ? response.statusCode : 200;
    const body = await responseText(response.response);
    if (statusCode >= 300) {
      throw new Error(`InvokeAgentRuntime returned HTTP ${statusCode}`);
    }
    if (!body) {
      throw new Error("InvokeAgentRuntime returned an empty response");
    }
    const result = JSON.parse(body) as { status?: string; error?: string };
    if (!["accepted", "duplicate"].includes(result.status ?? "")) {
      throw new Error(result.error ?? `Unexpected runtime status: ${result.status}`);
    }
  }

  return async (
    event: APIGatewayProxyEventV2,
  ): Promise<APIGatewayProxyStructuredResultV2> => {
    const rawBody = event.isBase64Encoded
      ? Buffer.from(event.body ?? "", "base64").toString("utf8")
      : event.body ?? "";
    const headers = event.headers ?? {};
    const signingSecret = await secretValue(
      requiredEnv("SLACK_SIGNING_SECRET_ARN"),
    );

    if (
      !verifySlackSignature(
        header(headers, "X-Slack-Signature"),
        header(headers, "X-Slack-Request-Timestamp"),
        rawBody,
        signingSecret,
        deps.now(),
      )
    ) {
      return { statusCode: 401, body: "invalid signature" };
    }

    let body: SlackCallback;
    try {
      body = JSON.parse(rawBody) as SlackCallback;
    } catch {
      return { statusCode: 400, body: "invalid json" };
    }

    if (body.type === "url_verification" && body.challenge) {
      return { statusCode: 200, body: body.challenge };
    }

    if (header(headers, "X-Slack-Retry-Num") !== undefined) {
      return { statusCode: 200, body: "ok (retry ignored)" };
    }

    const slackEvent = body.event;
    if (
      body.type !== "event_callback" ||
      slackEvent?.type !== "app_mention" ||
      slackEvent.bot_id ||
      slackEvent.app_id ||
      slackEvent.subtype === "bot_message"
    ) {
      return { statusCode: 200, body: "ok (ignored)" };
    }
    if (
      !body.event_id ||
      !body.team_id ||
      !slackEvent.channel ||
      !slackEvent.user ||
      !slackEvent.ts
    ) {
      return { statusCode: 200, body: "ok (incomplete)" };
    }
    const threadTs = slackEvent.thread_ts ?? slackEvent.ts;
    const token = await secretValue(requiredEnv("SLACK_BOT_TOKEN_ARN"));
    await addReaction(token, slackEvent.channel, slackEvent.ts, "eyes").catch(
      (error) => console.warn("Failed to add eyes reaction", error),
    );

    const runtimePayload = {
      source: "slack",
      event_id: body.event_id,
      prompt: stripMention(slackEvent.text ?? ""),
      slack: {
        team_id: body.team_id,
        channel_id: slackEvent.channel,
        thread_ts: threadTs,
        trigger_message_ts: slackEvent.ts,
        slack_user_id: slackEvent.user,
      },
    };

    try {
      await invokeRuntime(
        requiredEnv("AGENT_RUNTIME_ARN"),
        sessionIdFor(body.team_id, slackEvent.channel, threadTs),
        runtimePayload,
      );
    } catch (error) {
      console.error("AgentCore invocation failed", error);
      await setFailureStatus(
        token,
        slackEvent.channel,
        threadTs,
        slackEvent.ts,
      ).catch((reactionError) =>
        console.error("Failed to set failure reactions", reactionError),
      );
      await slackCall(token, "chat.postMessage", {
        channel: slackEvent.channel,
        thread_ts: threadTs,
        text: ":warning: I couldn't start that request. Please @codex me again to retry.",
        unfurl_links: false,
        unfurl_media: false,
      }).catch((postError) =>
        console.error("Failed to post invocation failure", postError),
      );
    }

    return { statusCode: 200, body: "ok" };
  };
}

export const handler = createHandler();
