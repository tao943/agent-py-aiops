import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const chatSource = readFileSync(fileURLToPath(new URL("../src/views/ChatView.vue", import.meta.url)), "utf8");
const configurationSource = readFileSync(fileURLToPath(new URL("../src/views/AgentConfigurationView.vue", import.meta.url)), "utf8");

describe("chat assembly ownership", () => {
  it("moves Prompt and Skill authoring out of Chat and links to the versioned control plane", () => {
    expect(chatSource).not.toContain("ChatPromptSidebar");
    expect(chatSource).not.toContain("ChatSkillSidebar");
    expect(chatSource).toContain("/agent-config?node=conversation");
    expect(configurationSource).toContain("ResourceLibrary");
    expect(configurationSource).toContain("VersionEditor");
    expect(configurationSource).toContain("BindingPanel");
  });
});
