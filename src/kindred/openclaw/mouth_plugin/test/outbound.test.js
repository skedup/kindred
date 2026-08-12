import assert from "node:assert/strict";
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { digest } from "../binding.js";
import {
  COMMIT_OUTBOUND_METHOD,
  commitOutbound,
  registerCommitOutbound,
} from "../outbound.js";

const OPERATION = "a".repeat(64);

function fixture() {
  const home = mkdtempSync(join(tmpdir(), "kindred-mouth-outbound-"));
  const configDir = join(home, ".config/kindred");
  const marker = join(home, "resident.json");
  mkdirSync(configDir, { recursive: true });
  writeFileSync(marker, JSON.stringify({ install_id: "install-a" }));
  writeFileSync(
    join(configDir, "openclaw-binding.json"),
    JSON.stringify({
      schema_version: 3,
      install_id: "install-a",
      agent_id: "agent-a",
      workspace_digest: digest("/synthetic/workspace"),
      session_key: "session-key-a",
      peer_scope: {
        message_provider: "channel",
        channel_id_digest: digest("peer-a"),
      },
      bundle_path: join(home, "bundle.md"),
      resident_marker_path: marker,
    }),
  );
  chmodSync(join(configDir, "openclaw-binding.json"), 0o600);
  return home;
}

function runtime(home, options = {}) {
  const messages = [];
  const events = [
    {
      type: "message",
      message: {
        role: "assistant",
        api: "openai-responses",
        provider: "openclaw",
        model: "delivery-mirror",
      },
    },
    {
      type: "message",
      message: {
        role: "assistant",
        api: "google-generative-ai",
        provider: "google",
        model: "gemini-synthetic",
      },
    },
  ];
  const sessionIds = [...(options.sessionIds ?? [])];
  const lockIdentities = [];
  let queue = Promise.resolve();
  const context = {
    readEvents: async () => events,
    appendMessage: async ({ message, idempotencyLookup }) => {
      assert.equal(idempotencyLookup, "scan");
      const existing = messages.find(
        (item) => item.message.idempotencyKey === message.idempotencyKey,
      );
      if (existing) return { appended: false, ...existing };
      const result = { message, messageId: `message-${messages.length + 1}` };
      messages.push(result);
      return { appended: true, ...result };
    },
    publishUpdate: async (update) => {
      context.update = update;
    },
  };
  return {
    home,
    messages,
    context,
    lockIdentities,
    getSessionEntry: (identity) => {
      assert.deepEqual(identity, { agentId: "agent-a", sessionKey: "session-key-a" });
      if (options.missingSession) return undefined;
      return { sessionId: sessionIds.shift() ?? options.sessionId ?? "session-id-a" };
    },
    withSessionTranscriptWriteLock: (identity, run) => {
      assert.equal(identity.agentId, "agent-a");
      assert.equal(identity.sessionKey, "session-key-a");
      assert.equal(typeof identity.sessionId, "string");
      lockIdentities.push(identity);
      const result = queue.then(() => run(context));
      queue = result.catch(() => undefined);
      return result;
    },
  };
}

test("commits one hidden replay-visible assistant with trusted metadata", async () => {
  const home = fixture();
  try {
    const deps = runtime(home);
    assert.deepEqual(
      await commitOutbound({ operation_id: OPERATION, text: "synthetic committed text" }, deps),
      { status: "committed" },
    );
    const message = deps.messages[0].message;
    assert.deepEqual(
      { api: message.api, provider: message.provider, model: message.model },
      {
        api: "google-generative-ai",
        provider: "google",
        model: "gemini-synthetic",
      },
    );
    assert.equal(message.display, false);
    assert.equal(message.idempotencyKey, `kindred-heart-context:${OPERATION}`);
    assert.deepEqual(deps.context.update, {
      message,
      messageId: "message-1",
    });
    assert.deepEqual(deps.messages.filter((item) => item.message.display !== false), []);
    assert.equal(deps.messages[0].message.content[0].text, "synthetic committed text");
  } finally {
    rmSync(home, { recursive: true });
  }
});

test("same operation is idempotent under the transcript write lock", async () => {
  const home = fixture();
  try {
    const deps = runtime(home);
    const params = { operation_id: OPERATION, text: "synthetic committed text" };
    const results = await Promise.all([commitOutbound(params, deps), commitOutbound(params, deps)]);
    assert.deepEqual(results, [{ status: "committed" }, { status: "already_done" }]);
    assert.equal(deps.messages.length, 1);
  } finally {
    rmSync(home, { recursive: true });
  }
});

test("an existing marker returns already_done without requiring old model metadata", async () => {
  const home = fixture();
  try {
    const deps = runtime(home);
    deps.context.readEvents = async () => [
      {
        type: "message",
        message: {
          role: "assistant",
          display: false,
          idempotencyKey: `kindred-heart-context:${OPERATION}`,
        },
      },
    ];
    assert.deepEqual(
      await commitOutbound({ operation_id: OPERATION, text: "synthetic" }, deps),
      { status: "already_done" },
    );
    assert.equal(deps.messages.length, 0);
  } finally {
    rmSync(home, { recursive: true });
  }
});

test("rejects extra request fields", async () => {
  const home = fixture();
  try {
    await assert.rejects(
      commitOutbound(
        { operation_id: OPERATION, text: "synthetic", sessionKey: "other" },
        runtime(home),
      ),
      /invalid commitOutbound request/,
    );
    await assert.rejects(
      commitOutbound({ operation_id: [OPERATION], text: "synthetic" }, runtime(home)),
      /invalid commitOutbound request/,
    );
  } finally {
    rmSync(home, { recursive: true });
  }
});

test("uses the current transcript generation without rebinding", async () => {
  const home = fixture();
  try {
    const deps = runtime(home, { sessionId: "session-id-after-new" });
    assert.deepEqual(
      await commitOutbound({ operation_id: OPERATION, text: "synthetic" }, deps),
      { status: "committed" },
    );
    assert.deepEqual(deps.lockIdentities, [
      {
        agentId: "agent-a",
        sessionKey: "session-key-a",
        sessionId: "session-id-after-new",
      },
    ]);
  } finally {
    rmSync(home, { recursive: true });
  }
});

test("rejects missing or malformed current transcript generations", async () => {
  for (const options of [
    { missingSession: true },
    { sessionIds: [" "] },
    { sessionIds: ["x".repeat(513)] },
  ]) {
    const home = fixture();
    try {
      const deps = runtime(home, options);
      await assert.rejects(
        commitOutbound({ operation_id: OPERATION, text: "synthetic" }, deps),
        /session is unavailable/,
      );
      assert.equal(deps.lockIdentities.length, 0);
      assert.equal(deps.messages.length, 0);
    } finally {
      rmSync(home, { recursive: true });
    }
  }
});

test("re-resolves once when the transcript rolls over before the lock", async () => {
  const home = fixture();
  try {
    const deps = runtime(home, {
      sessionIds: ["session-id-a", "session-id-b", "session-id-b", "session-id-b"],
    });
    assert.deepEqual(
      await commitOutbound({ operation_id: OPERATION, text: "synthetic" }, deps),
      { status: "committed" },
    );
    assert.deepEqual(
      deps.lockIdentities.map((identity) => identity.sessionId),
      ["session-id-a", "session-id-b"],
    );
    assert.equal(deps.messages.length, 1);
  } finally {
    rmSync(home, { recursive: true });
  }
});

test("fails closed when the transcript keeps rolling over", async () => {
  const home = fixture();
  try {
    const deps = runtime(home, {
      sessionIds: ["session-id-a", "session-id-b", "session-id-c", "session-id-d"],
    });
    await assert.rejects(
      commitOutbound({ operation_id: OPERATION, text: "synthetic" }, deps),
      /session rolled over/,
    );
    assert.equal(deps.messages.length, 0);
  } finally {
    rmSync(home, { recursive: true });
  }
});

test("fails closed when the transcript has no replay-eligible assistant metadata", async () => {
  const home = fixture();
  try {
    const deps = runtime(home);
    deps.context.readEvents = async () => [
      {
        type: "message",
        message: {
          role: "assistant",
          api: "openai-responses",
          provider: "openclaw",
          model: "delivery-mirror",
        },
      },
    ];
    await assert.rejects(
      commitOutbound({ operation_id: OPERATION, text: "synthetic" }, deps),
      /trusted assistant metadata is unavailable/,
    );
    assert.equal(deps.messages.length, 0);
  } finally {
    rmSync(home, { recursive: true });
  }
});

test("registers the fixed operator.admin RPC and returns sanitized failure", async () => {
  let registered;
  const api = {
    registerGatewayMethod(method, handler, options) {
      registered = { method, handler, options };
    },
  };
  registerCommitOutbound(api, {});
  assert.equal(registered.method, COMMIT_OUTBOUND_METHOD);
  assert.deepEqual(registered.options, { scope: "operator.admin" });
  let response;
  await registered.handler({
    params: { operation_id: "bad", text: "private text" },
    respond(...args) {
      response = args;
    },
  });
  assert.deepEqual(response, [
    false,
    undefined,
    { code: "UNAVAILABLE", message: "Kindred Mouth context commit failed" },
  ]);
  assert.equal(JSON.stringify(response).includes("private text"), false);
});
