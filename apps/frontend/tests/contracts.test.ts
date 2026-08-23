import { describe, expect, it } from "vitest";

import {
  describeSharedContractUsage,
  getFrontendFoundationHealth,
  getRequiredSseEventTypes
} from "../src/foundation";

describe("frontend contract consumption", () => {
  it("uses shared API response contracts", () => {
    const usage = describeSharedContractUsage();

    expect(usage.ok).toBe(true);
    expect(usage.data.service).toBe(getFrontendFoundationHealth().service);
    expect(usage.meta.requestId).toBe("frontend-foundation");
  });

  it("uses shared SSE event contracts", () => {
    expect(getRequiredSseEventTypes()).toEqual([
      "content.delta",
      "tool.call",
      "reference.source",
      "diagnostic.result",
      "run.status",
      "run.restarted",
      "execution.mode_selected",
      "structured.result",
      "confirmation.required",
      "confirmation.resolved",
      "explanation.delta",
      "explanation.degraded",
      "budget.exhausted",
      "task.status",
      "report",
      "complete",
      "error"
    ]);
  });
});
