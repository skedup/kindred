import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import { loadMouthContext } from "./binding.js";

export default definePluginEntry({
  id: "kindred-mouth",
  name: "Kindred Mouth",
  description: "Injects the current Kindred context into one approved direct peer.",
  register(api) {
    api.on("before_prompt_build", (_event, ctx) => {
      const context = loadMouthContext(ctx);
      return context ? { prependContext: context } : undefined;
    });
  },
});
