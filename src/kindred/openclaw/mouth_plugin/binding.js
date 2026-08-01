import { createHash } from "node:crypto";
import { lstatSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { isAbsolute, join } from "node:path";

const BINDING_KEYS = new Set([
  "schema_version",
  "install_id",
  "agent_id",
  "workspace_digest",
  "transcript_session_digest",
  "peer_scope",
  "bundle_path",
  "resident_marker_path",
]);
const PEER_KEYS = new Set(["message_provider", "channel_id_digest"]);
const DIGEST_RE = /^sha256:[0-9a-f]{64}$/;

export function digest(value) {
  return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value, expected) {
  const keys = Object.keys(value);
  return keys.length === expected.size && keys.every((key) => expected.has(key));
}

function readRegularJson(path) {
  if (!isAbsolute(path) || !lstatSync(path).isFile()) return null;
  const value = JSON.parse(readFileSync(path, "utf8"));
  return isRecord(value) ? value : null;
}

function isBinding(value) {
  if (!isRecord(value) || !hasExactKeys(value, BINDING_KEYS)) return false;
  const peer = value.peer_scope;
  return (
    value.schema_version === 1 &&
    typeof value.install_id === "string" &&
    value.install_id.length > 0 &&
    typeof value.agent_id === "string" &&
    value.agent_id.length > 0 &&
    DIGEST_RE.test(value.workspace_digest) &&
    DIGEST_RE.test(value.transcript_session_digest) &&
    isRecord(peer) &&
    hasExactKeys(peer, PEER_KEYS) &&
    typeof peer.message_provider === "string" &&
    peer.message_provider.length > 0 &&
    DIGEST_RE.test(peer.channel_id_digest) &&
    isAbsolute(value.bundle_path) &&
    isAbsolute(value.resident_marker_path)
  );
}

export function loadMouthContext(ctx, options = {}) {
  try {
    const home = options.home ?? homedir();
    const binding = readRegularJson(join(home, ".config/kindred/openclaw-binding.json"));
    if (!isBinding(binding) || ctx.sessionKey === "global") return null;
    const channelId = ctx.channelId ?? ctx.chatId;
    if (
      ctx.agentId !== binding.agent_id ||
      typeof ctx.workspaceDir !== "string" ||
      digest(ctx.workspaceDir) !== binding.workspace_digest ||
      typeof ctx.sessionKey !== "string" ||
      digest(ctx.sessionKey) !== binding.transcript_session_digest ||
      ctx.messageProvider !== binding.peer_scope.message_provider ||
      typeof channelId !== "string" ||
      digest(channelId) !== binding.peer_scope.channel_id_digest
    ) {
      return null;
    }
    const marker = readRegularJson(binding.resident_marker_path);
    if (marker?.install_id !== binding.install_id) return null;
    const bundle = readFileSync(binding.bundle_path, "utf8").trim();
    return bundle || null;
  } catch {
    return null;
  }
}
