import { describe, expect, it } from "vitest";
import { formatSessionLabel } from "../session-label";

describe("formatSessionLabel", () => {
  it("formats current napcat dm session keys", () => {
    expect(formatSessionLabel("agent:main:napcat:dm:10001")).toBe("DM:10001");
  });

  it("formats current napcat group session keys", () => {
    expect(formatSessionLabel("agent:main:napcat:group:123456")).toBe("Group:123456");
  });

  it("formats napcat group session keys with group names when available", () => {
    expect(formatSessionLabel("agent:main:napcat:group:123456", {
      chatType: "group",
      chatId: "123456",
      chatName: "测试群",
    })).toBe("Group: 测试群 (123456)");
  });

  it("formats legacy napcat private session keys", () => {
    expect(formatSessionLabel("napcat:private:10001")).toBe("DM:10001");
  });

  it("falls back to the raw session key for unknown formats", () => {
    expect(formatSessionLabel("session-a")).toBe("session-a");
  });
});
