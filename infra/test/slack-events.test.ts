import { createHmac } from "node:crypto";

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  createHandler,
  sessionIdFor,
  stripMention,
  verifySlackSignature,
} from "../functions/slack-events";

const NOW = 1_800_000_000_000;
const SIGNING_SECRET = "signing-secret";

function sign(body: string): Record<string, string> {
  const timestamp = String(NOW / 1000);
  const signature =
    "v0=" +
    createHmac("sha256", SIGNING_SECRET)
      .update(`v0:${timestamp}:${body}`)
      .digest("hex");
  return {
    "x-slack-request-timestamp": timestamp,
    "x-slack-signature": signature,
  };
}

function mentionBody(): string {
  return JSON.stringify({
    type: "event_callback",
    event_id: "Ev1",
    team_id: "T1",
    event: {
      type: "app_mention",
      user: "U1",
      channel: "C1",
      ts: "2.0",
      thread_ts: "1.0",
      text: "<@UBOT> fix it",
    },
  });
}

function makeDependencies(options: { runtimeFailure?: boolean } = {}) {
  const secretClient = {
    send: vi.fn(async (command: { input?: { SecretId?: string } }) => ({
      SecretString: command.input?.SecretId?.includes("signing")
        ? SIGNING_SECRET
        : "bot-token",
    })),
  };
  const runtimeClient = {
    send: vi.fn(async (_command: unknown) => {
      if (options.runtimeFailure) {
        throw new Error("runtime down");
      }
      return {
        statusCode: 200,
        response: Buffer.from(JSON.stringify({ status: "accepted" })),
      };
    }),
  };
  const fetchMock = vi.fn<typeof fetch>(async () =>
    Response.json({ ok: true, ts: "3.0" }),
  );
  return { secretClient, runtimeClient, fetchMock };
}

beforeEach(() => {
  process.env.SLACK_SIGNING_SECRET_ARN = "signing-secret-arn";
  process.env.SLACK_BOT_TOKEN_ARN = "bot-token-arn";
  process.env.AGENT_RUNTIME_ARN = "runtime-arn";
});

describe("Slack signature", () => {
  it("accepts a valid signature", () => {
    const body = "{}";
    const headers = sign(body);
    expect(
      verifySlackSignature(
        headers["x-slack-signature"],
        headers["x-slack-request-timestamp"],
        body,
        SIGNING_SECRET,
        NOW,
      ),
    ).toBe(true);
  });

  it("rejects stale requests", () => {
    expect(
      verifySlackSignature("v0=bad", "1", "{}", SIGNING_SECRET, NOW),
    ).toBe(false);
  });

  it("rejects a signature for a different body", () => {
    const headers = sign('{"value":1}');
    expect(
      verifySlackSignature(
        headers["x-slack-signature"],
        headers["x-slack-request-timestamp"],
        '{"value":2}',
        SIGNING_SECRET,
        NOW,
      ),
    ).toBe(false);
  });
});

describe("session IDs", () => {
  it("are stable, valid, and thread-specific", () => {
    const first = sessionIdFor("T1", "C1", "1.0");
    expect(first).toBe(sessionIdFor("T1", "C1", "1.0"));
    expect(first).not.toBe(sessionIdFor("T1", "C1", "2.0"));
    expect(first).toMatch(/^[A-Za-z0-9_-]{33,100}$/);
  });
});

describe("mention text", () => {
  it("removes only the leading app mention", () => {
    expect(stripMention("  <@UBOT> ask <@U123> to review this")).toBe(
      "ask <@U123> to review this",
    );
  });
});

describe("handler", () => {
  it("rejects invalid signatures before invoking Slack or AgentCore", async () => {
    const dependencies = makeDependencies();
    const handler = createHandler({
      secrets: dependencies.secretClient,
      agentCore: dependencies.runtimeClient,
      fetch: dependencies.fetchMock,
      now: () => NOW,
    });
    const result = await handler({
      version: "2.0",
      routeKey: "POST /slack/events",
      rawPath: "/slack/events",
      rawQueryString: "",
      headers: {
        "x-slack-request-timestamp": String(NOW / 1000),
        "x-slack-signature": "v0=invalid",
      },
      requestContext: {} as never,
      isBase64Encoded: false,
      body: mentionBody(),
    });

    expect(result.statusCode).toBe(401);
    expect(dependencies.runtimeClient.send).not.toHaveBeenCalled();
    expect(dependencies.fetchMock).not.toHaveBeenCalled();
  });

  it("handles Slack URL verification", async () => {
    const dependencies = makeDependencies();
    const body = JSON.stringify({
      type: "url_verification",
      challenge: "challenge-value",
    });
    const handler = createHandler({
      secrets: dependencies.secretClient,
      agentCore: dependencies.runtimeClient,
      fetch: dependencies.fetchMock,
      now: () => NOW,
    });
    const result = await handler({
      version: "2.0",
      routeKey: "POST /slack/events",
      rawPath: "/slack/events",
      rawQueryString: "",
      headers: sign(body),
      requestContext: {} as never,
      isBase64Encoded: false,
      body,
    });
    expect(result).toEqual({ statusCode: 200, body: "challenge-value" });
  });

  it("uses the current signing secret after rotation", async () => {
    let signingSecret = "first-secret";
    const secretClient = {
      send: vi.fn(async () => ({ SecretString: signingSecret })),
    };
    const dependencies = makeDependencies();
    const handler = createHandler({
      secrets: secretClient,
      agentCore: dependencies.runtimeClient,
      fetch: dependencies.fetchMock,
      now: () => NOW,
    });

    async function verify(secret: string, challenge: string) {
      const body = JSON.stringify({ type: "url_verification", challenge });
      const timestamp = String(NOW / 1000);
      const signature =
        "v0=" +
        createHmac("sha256", secret)
          .update(`v0:${timestamp}:${body}`)
          .digest("hex");
      return handler({
        version: "2.0",
        routeKey: "POST /slack/events",
        rawPath: "/slack/events",
        rawQueryString: "",
        headers: {
          "x-slack-request-timestamp": timestamp,
          "x-slack-signature": signature,
        },
        requestContext: {} as never,
        isBase64Encoded: false,
        body,
      });
    }

    expect(await verify(signingSecret, "first")).toEqual({
      statusCode: 200,
      body: "first",
    });
    signingSecret = "second-secret";
    expect(await verify(signingSecret, "second")).toEqual({
      statusCode: 200,
      body: "second",
    });
    expect(secretClient.send).toHaveBeenCalledTimes(2);
  });

  it("adds eyes and invokes AgentCore with the thread session", async () => {
    const dependencies = makeDependencies();
    const body = mentionBody();
    const handler = createHandler({
      secrets: dependencies.secretClient,
      agentCore: dependencies.runtimeClient,
      fetch: dependencies.fetchMock,
      now: () => NOW,
    });
    const result = await handler({
      version: "2.0",
      routeKey: "POST /slack/events",
      rawPath: "/slack/events",
      rawQueryString: "",
      headers: sign(body),
      requestContext: {} as never,
      isBase64Encoded: false,
      body,
    });

    expect(result).toEqual({ statusCode: 200, body: "ok" });
    expect(dependencies.fetchMock).toHaveBeenCalledWith(
      "https://slack.com/api/reactions.add",
      expect.objectContaining({
        body: JSON.stringify({
          channel: "C1",
          timestamp: "2.0",
          name: "eyes",
        }),
      }),
    );
    const command = dependencies.runtimeClient.send.mock.calls[0]![0] as {
      input: {
        runtimeSessionId: string;
        payload: Uint8Array;
      };
    };
    expect(command.input.runtimeSessionId).toBe(sessionIdFor("T1", "C1", "1.0"));
    expect(JSON.parse(Buffer.from(command.input.payload).toString("utf8"))).toEqual({
      source: "slack",
      event_id: "Ev1",
      prompt: "fix it",
      slack: {
        team_id: "T1",
        channel_id: "C1",
        thread_ts: "1.0",
        trigger_message_ts: "2.0",
        slack_user_id: "U1",
      },
    });
  });

  it("ignores Slack retry deliveries", async () => {
    const dependencies = makeDependencies();
    const body = mentionBody();
    const handler = createHandler({
      secrets: dependencies.secretClient,
      agentCore: dependencies.runtimeClient,
      fetch: dependencies.fetchMock,
      now: () => NOW,
    });
    const result = await handler({
      version: "2.0",
      routeKey: "POST /slack/events",
      rawPath: "/slack/events",
      rawQueryString: "",
      headers: { ...sign(body), "x-slack-retry-num": "1" },
      requestContext: {} as never,
      isBase64Encoded: false,
      body,
    });
    expect(result.body).toBe("ok (retry ignored)");
    expect(dependencies.runtimeClient.send).not.toHaveBeenCalled();
  });

  it("ignores bot-authored app mentions", async () => {
    const dependencies = makeDependencies();
    const parsed = JSON.parse(mentionBody());
    parsed.event.bot_id = "B1";
    const body = JSON.stringify(parsed);
    const handler = createHandler({
      secrets: dependencies.secretClient,
      agentCore: dependencies.runtimeClient,
      fetch: dependencies.fetchMock,
      now: () => NOW,
    });
    const result = await handler({
      version: "2.0",
      routeKey: "POST /slack/events",
      rawPath: "/slack/events",
      rawQueryString: "",
      headers: sign(body),
      requestContext: {} as never,
      isBase64Encoded: false,
      body,
    });

    expect(result.body).toBe("ok (ignored)");
    expect(dependencies.runtimeClient.send).not.toHaveBeenCalled();
    expect(dependencies.fetchMock).not.toHaveBeenCalled();
  });

  it("marks failures red and posts a fallback", async () => {
    const dependencies = makeDependencies({ runtimeFailure: true });
    const body = mentionBody();
    const handler = createHandler({
      secrets: dependencies.secretClient,
      agentCore: dependencies.runtimeClient,
      fetch: dependencies.fetchMock,
      now: () => NOW,
    });
    await handler({
      version: "2.0",
      routeKey: "POST /slack/events",
      rawPath: "/slack/events",
      rawQueryString: "",
      headers: sign(body),
      requestContext: {} as never,
      isBase64Encoded: false,
      body,
    });
    const urls = dependencies.fetchMock.mock.calls.map(([url]) => url);
    expect(urls).toContain("https://slack.com/api/chat.postMessage");
    expect(
      dependencies.fetchMock.mock.calls.some(
        ([url, init]) =>
          url === "https://slack.com/api/reactions.add" &&
          String(init?.body).includes('"red_circle"'),
      ),
    ).toBe(true);
    const redTargets = dependencies.fetchMock.mock.calls
      .filter(
        ([url, init]) =>
          url === "https://slack.com/api/reactions.add" &&
          JSON.parse(String(init?.body)).name === "red_circle",
      )
      .map(([, init]) => JSON.parse(String(init?.body)).timestamp);
    expect(redTargets).toEqual(["1.0", "2.0"]);
  });
});
