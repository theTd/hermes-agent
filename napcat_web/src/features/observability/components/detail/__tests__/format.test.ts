import { describe, expect, it } from "vitest";

import {
  getExpandableTextPanelVisibility,
  looksLikeStructuredPayloadText,
} from "../format";

describe("looksLikeStructuredPayloadText", () => {
  it("detects pure json payload text", () => {
    expect(looksLikeStructuredPayloadText("{\"messages\":[\"hello\"]}")).toBe(true);
  });

  it("detects mixed debug heading plus json payload text", () => {
    expect(looksLikeStructuredPayloadText("## lightrag\n{\n  \"lane\": \"identity\"\n}")).toBe(true);
  });

  it("does not classify plain readable text as payload", () => {
    expect(looksLikeStructuredPayloadText("<memory-context>Alice</memory-context>")).toBe(false);
    expect(looksLikeStructuredPayloadText("Plain English summary of what happened")).toBe(false);
  });
});

describe("getExpandableTextPanelVisibility", () => {
  it("keeps thinking and response visible", () => {
    expect(getExpandableTextPanelVisibility({
      title: "Thinking",
      label: "req-1.thinking",
      content: "Reasoning text",
    })).toBe("preview");
    expect(getExpandableTextPanelVisibility({
      title: "Response",
      label: "req-1.response",
      content: "Final answer",
    })).toBe("preview");
  });

  it("hides request bodies and debug payload panels", () => {
    expect(getExpandableTextPanelVisibility({
      title: "Request Body",
      label: "req-1.request",
      content: "{\"messages\":[\"hello\"]}",
    })).toBe("hidden");
    expect(getExpandableTextPanelVisibility({
      title: "Debug Payload",
      label: "agent.memory.used",
      content: "{\n  \"source\": \"auto_injection\"\n}",
    })).toBe("hidden");
  });

  it("hides mixed context parameter payloads while leaving readable context visible", () => {
    expect(getExpandableTextPanelVisibility({
      title: "Captured Context",
      label: "1.agent.memory.used.memory_prefetch_params_by_provider",
      content: "## lightrag\n{\n  \"lane\": \"identity\"\n}",
    })).toBe("hidden");
    expect(getExpandableTextPanelVisibility({
      title: "Captured Context",
      label: "1.agent.memory.used.memory_prefetch_fenced",
      content: "<memory-context>Alice</memory-context>",
    })).toBe("preview");
  });
});
