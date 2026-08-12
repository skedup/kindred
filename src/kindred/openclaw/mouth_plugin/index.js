import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { getSessionEntry } from "openclaw/plugin-sdk/session-store-runtime";
import { withSessionTranscriptWriteLock } from "openclaw/plugin-sdk/session-transcript-runtime";

import { loadMouthContext } from "./binding.js";
import { registerCommitOutbound } from "./outbound.js";

export default definePluginEntry({
  id: "kindred-mouth",
  name: "Kindred Mouth",
  description: "Injects the current Kindred context into one approved direct peer.",
  register(api) {
    api.on("before_prompt_build", (_event, ctx) => {
      const context = loadMouthContext(ctx);
      return context ? { prependContext: context } : undefined;
    });
    registerCommitOutbound(api, { getSessionEntry, withSessionTranscriptWriteLock });
  },
});
