import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { basename } from "node:path";

import {
  BedrockAgentCoreClient,
  InvokeAgentRuntimeCommand,
} from "@aws-sdk/client-bedrock-agentcore";

interface Arguments {
  runtimeArn: string;
  session: string;
  prompt: string;
  user: string;
  attachments: string[];
  check: boolean;
}

interface TestResult {
  status?: string;
  thread_status?: string;
  replied?: boolean;
  waiting?: boolean;
  command_failures?: number;
  slack?: {
    posts?: Array<{
      text?: string;
    }>;
    reactions?: Array<{
      action?: string;
      emoji?: string;
    }>;
  };
}

const USAGE =
  "Usage: npm run invoke:test -- --runtime-arn ARN --session NAME " +
  '--prompt "request" [--attach PATH] [--user USER] [--check]';

function valueAfter(args: string[], name: string): string | undefined {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : undefined;
}

function valuesAfter(args: string[], name: string): string[] {
  const values: string[] = [];
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] === name && args[index + 1]) {
      values.push(args[index + 1]!);
      index += 1;
    }
  }
  return values;
}

function parseArguments(argv: string[]): Arguments {
  const check = argv.includes("--check");
  const runtimeArn =
    valueAfter(argv, "--runtime-arn") ?? process.env.AGENT_RUNTIME_ARN ?? "";
  const session =
    valueAfter(argv, "--session") ?? (check ? `smoke-${Date.now()}` : "default");
  const prompt = valueAfter(argv, "--prompt") ?? "";
  const user = valueAfter(argv, "--user") ?? "local-user";
  if (!runtimeArn || !prompt) {
    throw new Error(USAGE);
  }
  return {
    runtimeArn,
    session,
    prompt,
    user,
    attachments: valuesAfter(argv, "--attach"),
    check,
  };
}

function testSessionId(name: string): string {
  const digest = createHash("sha256").update(`test\0${name}`).digest("hex");
  return `test_${digest}`;
}

async function responseText(value: unknown): Promise<string> {
  if (typeof value === "string") {
    return value;
  }
  if (value instanceof Uint8Array) {
    return Buffer.from(value).toString("utf8");
  }
  const transform = (value as { transformToString?: () => Promise<string> })
    ?.transformToString;
  return transform ? transform.call(value) : "";
}

function validateSmokeResult(result: TestResult): void {
  if (result.status !== "completed") {
    throw new Error(`Smoke test status was ${result.status ?? "missing"}`);
  }
  if (!result.replied || result.waiting || result.thread_status !== "done") {
    throw new Error("Smoke test did not finish with one successful Slack reply");
  }
  if (result.command_failures !== 0) {
    throw new Error(
      `Smoke test observed ${result.command_failures ?? "unknown"} failed shell commands`,
    );
  }
  if (result.slack?.posts?.length !== 1) {
    throw new Error(
      `Smoke test expected one Slack reply, received ${result.slack?.posts?.length ?? 0}`,
    );
  }
  if (result.slack.posts[0]?.text !== "AgentCore smoke test passed") {
    throw new Error("Smoke test did not post the expected success message");
  }
  const terminalReaction = result.slack.reactions
    ?.filter(
      (event) =>
        event.action === "add" &&
        ["large_yellow_circle", "question", "large_green_circle", "red_circle"].includes(
          event.emoji ?? "",
        ),
    )
    .at(-1);
  if (terminalReaction?.emoji !== "large_green_circle") {
    throw new Error("Smoke test did not finish with a green thread status");
  }
}

async function main(): Promise<void> {
  const argv = process.argv.slice(2);
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(USAGE);
    return;
  }
  const args = parseArguments(argv);
  const region = process.env.AWS_REGION ?? "us-east-1";
  const client = new BedrockAgentCoreClient({ region });
  const payload = {
    source: "test",
    event_id: `test-${randomUUID()}`,
    prompt: args.prompt,
    user_id: args.user,
    attachments: args.attachments.map((path) => ({
      name: basename(path),
      content_base64: readFileSync(path).toString("base64"),
      mimetype: "application/octet-stream",
    })),
  };

  const response = await client.send(
    new InvokeAgentRuntimeCommand({
      agentRuntimeArn: args.runtimeArn,
      runtimeSessionId: testSessionId(args.session),
      payload: Buffer.from(JSON.stringify(payload)),
      contentType: "application/json",
      accept: "application/json",
    }),
  );
  const body = await responseText(response.response);
  if ((response.statusCode ?? 200) >= 300) {
    throw new Error(`AgentCore returned HTTP ${response.statusCode}: ${body}`);
  }
  if (!body) {
    throw new Error("AgentCore returned an empty response");
  }
  const result = JSON.parse(body) as TestResult;
  console.log(JSON.stringify(result, null, 2));
  if (args.check) {
    validateSmokeResult(result);
  }
  if (args.check) {
    console.log("AgentCore smoke checks passed.");
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
