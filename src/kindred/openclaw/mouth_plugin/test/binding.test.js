import assert from "node:assert/strict";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { digest, loadMouthContext } from "../binding.js";

function fixture() {
  const home = mkdtempSync(join(tmpdir(), "kindred-mouth-"));
  const configDir = join(home, ".config/kindred");
  mkdirSync(configDir, { recursive: true });
  const bundle = join(home, "context-bundle.md");
  const marker = join(home, "resident.json");
  const ctx = {
    agentId: "resident",
    workspaceDir: join(home, "workspace"),
    sessionKey: "synthetic-session-a",
    messageProvider: "telegram",
    channelId: "synthetic-peer-a",
  };
  writeFileSync(bundle, "synthetic bundle\n");
  writeFileSync(marker, JSON.stringify({ install_id: "install-a" }));
  writeFileSync(
    join(configDir, "openclaw-binding.json"),
    JSON.stringify({
      schema_version: 1,
      install_id: "install-a",
      agent_id: ctx.agentId,
      workspace_digest: digest(ctx.workspaceDir),
      transcript_session_digest: digest(ctx.sessionKey),
      peer_scope: {
        message_provider: ctx.messageProvider,
        channel_id_digest: digest(ctx.channelId),
      },
      bundle_path: bundle,
      resident_marker_path: marker,
    }),
  );
  return { home, ctx, bundle, marker };
}

test("only the approved peer receives the bundle", () => {
  const f = fixture();
  try {
    assert.equal(loadMouthContext(f.ctx, { home: f.home }), "synthetic bundle");
    assert.equal(
      loadMouthContext({ ...f.ctx, channelId: "synthetic-peer-b" }, { home: f.home }),
      null,
    );
    assert.equal(
      loadMouthContext({ ...f.ctx, sessionKey: "newer-session" }, { home: f.home }),
      null,
    );
    assert.equal(
      loadMouthContext({ ...f.ctx, messageProvider: "other" }, { home: f.home }),
      null,
    );
    assert.equal(
      loadMouthContext({ ...f.ctx, workspaceDir: "other" }, { home: f.home }),
      null,
    );
    assert.equal(
      loadMouthContext({ ...f.ctx, agentId: "other" }, { home: f.home }),
      null,
    );
    assert.equal(loadMouthContext({ ...f.ctx, sessionKey: "global" }, { home: f.home }), null);
  } finally {
    rmSync(f.home, { recursive: true });
  }
});

test("binding and bundle failures degrade without interrupting the mouth", () => {
  const f = fixture();
  try {
    writeFileSync(f.marker, JSON.stringify({ install_id: "other" }));
    assert.equal(loadMouthContext(f.ctx, { home: f.home }), null);
    writeFileSync(f.marker, JSON.stringify({ install_id: "install-a" }));
    writeFileSync(f.bundle, " \n");
    assert.equal(loadMouthContext(f.ctx, { home: f.home }), null);
    rmSync(f.bundle);
    assert.equal(loadMouthContext(f.ctx, { home: f.home }), null);
    writeFileSync(f.bundle, "synthetic bundle\n");
    writeFileSync(join(f.home, ".config/kindred/openclaw-binding.json"), "{");
    assert.equal(loadMouthContext(f.ctx, { home: f.home }), null);
  } finally {
    rmSync(f.home, { recursive: true });
  }
});

test("binding rejects unknown fields and symlinked control files", () => {
  const f = fixture();
  try {
    const bindingPath = join(f.home, ".config/kindred/openclaw-binding.json");
    const binding = JSON.parse(readFileSync(bindingPath, "utf8"));
    writeFileSync(bindingPath, JSON.stringify({ ...binding, enabled: true }));
    assert.equal(loadMouthContext(f.ctx, { home: f.home }), null);

    writeFileSync(bindingPath, JSON.stringify(binding));
    const markerTarget = `${f.marker}.target`;
    rmSync(f.marker);
    writeFileSync(markerTarget, JSON.stringify({ install_id: "install-a" }));
    symlinkSync(markerTarget, f.marker);
    assert.equal(loadMouthContext(f.ctx, { home: f.home }), null);
  } finally {
    rmSync(f.home, { recursive: true });
  }
});
