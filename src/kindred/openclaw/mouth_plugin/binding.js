import { createHash } from "node:crypto";
import { closeSync, constants, fstatSync, openSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { isAbsolute, join } from "node:path";

const BINDING_KEYS = new Set([
  "schema_version",
  "install_id",
  "agent_id",
  "workspace_digest",
  "session_key",
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

function readRegularJson(path, { privateFile = false } = {}) {
  if (!isAbsolute(path)) return null;
  let fd;
  try {
    fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    const info = fstatSync(fd);
    if (
      !info.isFile() ||
      (privateFile &&
        (typeof process.getuid !== "function" ||
          info.uid !== process.getuid() ||
          (info.mode & 0o077) !== 0))
    ) {
      return null;
    }
    const value = JSON.parse(readFileSync(fd, "utf8"));
    return isRecord(value) ? value : null;
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}

function isBinding(value) {
  if (!isRecord(value) || !hasExactKeys(value, BINDING_KEYS)) return false;
  const peer = value.peer_scope;
  return (
    value.schema_version === 3 &&
    typeof value.install_id === "string" &&
    value.install_id.length > 0 &&
    typeof value.agent_id === "string" &&
    value.agent_id.length > 0 &&
    typeof value.workspace_digest === "string" &&
    DIGEST_RE.test(value.workspace_digest) &&
    typeof value.session_key === "string" &&
    value.session_key.length > 0 &&
    value.session_key.length <= 1024 &&
    isRecord(peer) &&
    hasExactKeys(peer, PEER_KEYS) &&
    typeof peer.message_provider === "string" &&
    peer.message_provider.length > 0 &&
    typeof peer.channel_id_digest === "string" &&
    DIGEST_RE.test(peer.channel_id_digest) &&
    isAbsolute(value.bundle_path) &&
    isAbsolute(value.resident_marker_path)
  );
}

export function readMouthBinding(options = {}) {
  try {
    const home = options.home ?? homedir();
    const binding = readRegularJson(join(home, ".config/kindred/openclaw-binding.json"), {
      privateFile: true,
    });
    if (!isBinding(binding)) return null;
    const marker = readRegularJson(binding.resident_marker_path);
    return marker?.install_id === binding.install_id ? binding : null;
  } catch {
    return null;
  }
}

export function loadMouthContext(ctx, options = {}) {
  try {
    const binding = readMouthBinding(options);
    if (!binding || ctx.sessionKey === "global") return null;
    const channelId = ctx.channelId ?? ctx.chatId;
    const channelMatches =
      typeof channelId === "string" &&
      [channelId, `${ctx.messageProvider}:${channelId}`].some(
        (candidate) => digest(candidate) === binding.peer_scope.channel_id_digest,
      );
    if (
      ctx.agentId !== binding.agent_id ||
      typeof ctx.workspaceDir !== "string" ||
      digest(ctx.workspaceDir) !== binding.workspace_digest ||
      ctx.sessionKey !== binding.session_key ||
      ctx.messageProvider !== binding.peer_scope.message_provider ||
      !channelMatches
    ) {
      return null;
    }
    const bundle = readFileSync(binding.bundle_path, "utf8").trim();
    return bundle || null;
  } catch {
    return null;
  }
}
