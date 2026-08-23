// @vitest-environment jsdom

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { afterEach, describe, expect, it } from "vitest";

import AppBadge from "../src/ui/AppBadge.vue";
import AppButton from "../src/ui/AppButton.vue";
import AppDrawer from "../src/ui/AppDrawer.vue";
import AppSkeleton from "../src/ui/AppSkeleton.vue";
import AppTabs from "../src/ui/AppTabs.vue";

afterEach(() => {
  document.body.innerHTML = "";
});

describe("workbench design system", () => {
  it("publishes semantic tokens, keyboard focus, and reduced motion", () => {
    const styles = readFileSync(
      resolve(process.cwd(), "src/styles.css"),
      "utf8"
    );

    for (const token of [
      "--surface-canvas",
      "--surface-panel",
      "--nav-bg",
      "--text-primary",
      "--accent",
      "--info",
      "--warning",
      "--danger",
      "--focus-ring"
    ]) {
      expect(styles).toContain(token);
    }
    expect(styles).toContain(":focus-visible");
    expect(styles).toContain("prefers-reduced-motion: reduce");
    expect(styles).not.toMatch(/focus-visible[^}]*outline:\s*(?:0|none)/s);
  });

  it("renders typed button, badge, and skeleton states", () => {
    const button = mount(AppButton, {
      props: { variant: "primary", loading: true },
      slots: { default: "保存" }
    });
    const badge = mount(AppBadge, { props: { tone: "warning" }, slots: { default: "待审批" } });
    const skeleton = mount(AppSkeleton, { props: { label: "正在加载事件" } });

    expect(button.get("button").attributes("aria-busy")).toBe("true");
    expect(button.get("button").attributes("disabled")).toBeDefined();
    expect(badge.get("span").attributes("data-tone")).toBe("warning");
    expect(skeleton.get('[role="status"]').attributes("aria-label")).toBe("正在加载事件");
  });

  it("exposes tab semantics and emits a selected tab", async () => {
    const wrapper = mount(AppTabs, {
      props: {
        modelValue: "evidence",
        label: "调查详情",
        items: [
          { id: "evidence", label: "证据" },
          { id: "recovery", label: "恢复" }
        ]
      }
    });

    expect(wrapper.get('[role="tablist"]').attributes("aria-label")).toBe("调查详情");
    expect(wrapper.get('[role="tab"][aria-selected="true"]').text()).toBe("证据");
    await wrapper.findAll('[role="tab"]')[1]?.trigger("click");
    expect(wrapper.emitted("update:modelValue")?.[0]).toEqual(["recovery"]);
  });

  it("closes the drawer on Escape and restores trigger focus", async () => {
    const trigger = document.createElement("button");
    document.body.append(trigger);
    trigger.focus();
    const wrapper = mount(AppDrawer, {
      attachTo: document.body,
      props: { open: true, title: "筛选事件", returnFocusTo: trigger },
      slots: { default: "筛选内容" },
      global: { stubs: { Teleport: true } }
    });

    expect(wrapper.get('[role="dialog"]').attributes("aria-modal")).toBe("true");
    await wrapper.get('[role="dialog"]').trigger("keydown", { key: "Escape" });
    expect(wrapper.emitted("close")).toHaveLength(1);
    await wrapper.setProps({ open: false });
    await nextTick();
    expect(document.activeElement).toBe(trigger);
  });
});
