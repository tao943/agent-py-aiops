// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import type { PendingChatAction } from "@agent-py/api-contracts";
import PendingChatActionCard from "../src/components/PendingChatActionCard.vue";

const action: PendingChatAction = {
  id: "chat_action_1",
  sessionId: "session_1",
  actionType: "create_recovery_approval",
  targetResourceId: "diagnostic_1",
  publicArguments: { reason: "请人工复核" },
  status: "pending",
  expiresAt: "2026-08-22T00:15:00Z",
  backgroundJobId: null,
  executionResultId: null
};

describe("PendingChatActionCard", () => {
  it("states the safe action boundary and emits one explicit decision", async () => {
    const wrapper = mount(PendingChatActionCard, {
      props: { action, isLoading: false }
    });

    expect(wrapper.text()).toContain("创建人工审批请求");
    expect(wrapper.text()).toContain("不会批准或执行恢复");
    expect(wrapper.text()).toContain("diagnostic_1");

    await wrapper.get('[data-action="confirm"]').trigger("click");
    await wrapper.get('[data-action="cancel"]').trigger("click");

    expect(wrapper.emitted("confirm")).toEqual([["chat_action_1"]]);
    expect(wrapper.emitted("cancel")).toEqual([["chat_action_1"]]);
  });

  it("disables both decisions while a request is in flight", () => {
    const wrapper = mount(PendingChatActionCard, {
      props: { action, isLoading: true }
    });

    expect(wrapper.get('[data-action="confirm"]').attributes("disabled")).toBeDefined();
    expect(wrapper.get('[data-action="cancel"]').attributes("disabled")).toBeDefined();
  });
});
