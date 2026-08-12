import { readMouthBinding } from "./binding.js";

export const COMMIT_OUTBOUND_METHOD = "kindred.mouth.commitOutbound";

const OPERATION_RE = /^[0-9a-f]{64}$/;
const PARAM_KEYS = new Set(["operation_id", "text"]);
const TRANSCRIPT_ONLY_MODELS = new Set(["delivery-mirror", "gateway-injected"]);

class SessionRolloverError extends Error {}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isOperationId(value) {
  return typeof value === "string" && OPERATION_RE.test(value);
}

function isNonEmptyString(value) {
  return typeof value === "string" && value.length > 0 && value === value.trim();
}

function isSessionId(value) {
  return isNonEmptyString(value) && value.length <= 512;
}

function validateParams(params) {
  if (
    !isRecord(params) ||
    Object.keys(params).length !== PARAM_KEYS.size ||
    !Object.keys(params).every((key) => PARAM_KEYS.has(key)) ||
    !isOperationId(params.operation_id) ||
    typeof params.text !== "string" ||
    !params.text.trim() ||
    params.text.length > 20_000
  ) {
    throw new Error("invalid commitOutbound request");
  }
}

function latestAssistantMetadata(events) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index]?.type !== "message") continue;
    const message = events[index]?.message;
    if (
      !isRecord(message) ||
      message.role !== "assistant" ||
      message.display === false ||
      (message.provider === "openclaw" && TRANSCRIPT_ONLY_MODELS.has(message.model)) ||
      !isNonEmptyString(message.api) ||
      !isNonEmptyString(message.provider) ||
      !isNonEmptyString(message.model)
    ) {
      continue;
    }
    return { api: message.api, provider: message.provider, model: message.model };
  }
  throw new Error("trusted assistant metadata is unavailable");
}

export async function commitOutbound(params, dependencies) {
  validateParams(params);
  const binding = readMouthBinding({ home: dependencies.home });
  if (!binding) throw new Error("Mouth binding is unavailable");

  const identity = { agentId: binding.agent_id, sessionKey: binding.session_key };
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const sessionId = dependencies.getSessionEntry(identity)?.sessionId;
    if (!isSessionId(sessionId)) throw new Error("Mouth session is unavailable");
    try {
      return await dependencies.withSessionTranscriptWriteLock(
        { ...identity, sessionId },
        async ({ appendMessage, publishUpdate, readEvents }) => {
          if (dependencies.getSessionEntry(identity)?.sessionId !== sessionId) {
            throw new SessionRolloverError("Mouth session rolled over");
          }
          const marker = `kindred-heart-context:${params.operation_id}`;
          const events = await readEvents();
          if (
            events.some(
              (event) =>
                event?.message?.role === "assistant" && event.message.idempotencyKey === marker,
            )
          ) {
            return { status: "already_done" };
          }
          const metadata = latestAssistantMetadata(events);
          const message = {
            role: "assistant",
            content: [{ type: "text", text: params.text }],
            ...metadata,
            display: false,
            idempotencyKey: marker,
            stopReason: "stop",
            timestamp: Date.now(),
          };
          const result = await appendMessage({ message, idempotencyLookup: "scan" });
          if (!result) throw new Error("Mouth transcript append failed");
          if (!result.appended) return { status: "already_done" };
          await publishUpdate({ message: result.message, messageId: result.messageId });
          return { status: "committed" };
        },
      );
    } catch (error) {
      if (!(error instanceof SessionRolloverError) || attempt > 0) throw error;
    }
  }
  throw new Error("Mouth session is unavailable");
}

export function registerCommitOutbound(api, dependencies) {
  api.registerGatewayMethod(
    COMMIT_OUTBOUND_METHOD,
    async ({ params, respond }) => {
      try {
        respond(true, await commitOutbound(params, dependencies));
      } catch {
        respond(false, undefined, {
          code: "UNAVAILABLE",
          message: "Kindred Mouth context commit failed",
        });
      }
    },
    { scope: "operator.admin" },
  );
}
