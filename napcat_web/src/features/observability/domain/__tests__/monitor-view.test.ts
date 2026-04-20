import { describe, expect, it } from "vitest";
import { buildSessionViewModels, buildTraceListItems } from "../monitor-view";
import { mapTraceToLiveGroup } from "../../stage-mapper";

function createGroup(traceId: string, sessionKey: string, startedAt: number) {
  const isNapcatDm = sessionKey.includes(":dm:");
  const isNapcatGroup = sessionKey.includes(":group:");
  const chatType = isNapcatDm ? "private" : "group";
  const chatId = isNapcatGroup ? "123456" : isNapcatDm ? "10001" : "chat-1";
  const chatName = isNapcatGroup ? "测试群" : isNapcatDm ? "测试好友" : "";

  return mapTraceToLiveGroup(
    {
      trace_id: traceId,
      session_key: sessionKey,
      session_id: `${sessionKey}-id`,
      chat_type: chatType,
      chat_id: chatId,
      user_id: "user-1",
      message_id: `${traceId}-msg`,
      started_at: startedAt,
      ended_at: startedAt + 1,
      event_count: 2,
      status: "completed",
      latest_stage: "final",
      summary: {
        headline: `${traceId} headline`,
        chat_name: chatName,
      },
    },
    [
      {
        event_id: `${traceId}-evt-1`,
        trace_id: traceId,
        span_id: `${traceId}-span-1`,
        parent_span_id: "",
        ts: startedAt,
        session_key: sessionKey,
        session_id: `${sessionKey}-id`,
        platform: "napcat",
        chat_type: chatType,
        chat_id: chatId,
        user_id: "user-1",
        message_id: `${traceId}-msg`,
        event_type: "message.received",
        severity: "info",
        payload: {
          text_preview: `${traceId} inbound`,
          chat_name: chatName,
          stage: "inbound",
        },
      },
      {
        event_id: `${traceId}-evt-2`,
        trace_id: traceId,
        span_id: `${traceId}-span-2`,
        parent_span_id: "",
        ts: startedAt + 1,
        session_key: sessionKey,
        session_id: `${sessionKey}-id`,
        platform: "napcat",
        chat_type: chatType,
        chat_id: chatId,
        user_id: "user-1",
        message_id: `${traceId}-msg`,
        event_type: "agent.response.final",
        severity: "info",
        payload: { response_preview: `${traceId} done`, stage: "final" },
      },
    ],
  );
}

describe("monitor view models", () => {
  it("builds session summaries with selection state", () => {
    const groups = [
      createGroup("trace-a1", "session-a", 10),
      createGroup("trace-a2", "session-a", 20),
      createGroup("trace-b1", "session-b", 30),
    ];

    const sessions = buildSessionViewModels(groups, [], "session-a");

    expect(sessions).toHaveLength(2);
    expect(sessions[0]).toEqual(expect.objectContaining({
      sessionKey: "session-b",
      displayTitle: "session-b",
      traceCount: 1,
      selected: false,
    }));
    expect(sessions[1]).toEqual(expect.objectContaining({
      sessionKey: "session-a",
      displayTitle: "session-a",
      traceCount: 2,
      selected: true,
    }));
  });

  it("formats napcat session titles for the session rail", () => {
    const groups = [
      createGroup("trace-dm", "agent:main:napcat:dm:10001", 10),
      createGroup("trace-group", "agent:main:napcat:group:123456", 20),
    ];

    const sessions = buildSessionViewModels(groups, [], null);

    expect(sessions[0]?.displayTitle).toBe("Group: 测试群 (123456)");
    expect(sessions[1]?.displayTitle).toBe("DM:10001");
  });

  it("builds trace list items scoped to a session", () => {
    const groups = [
      createGroup("trace-a1", "session-a", 10),
      createGroup("trace-b1", "session-b", 30),
    ];

    const items = buildTraceListItems(groups, "trace-a1", "session-a");

    expect(items).toHaveLength(1);
    expect(items[0]?.group.traceId).toBe("trace-a1");
    expect(items[0]?.selected).toBe(true);
  });

  it("keeps early traces without a session key visible while a session is selected", () => {
    const groups = [
      createGroup("trace-a1", "session-a", 10),
      createGroup("trace-early", "", 40),
    ];

    const items = buildTraceListItems(groups, null, "session-a");

    expect(items.map((item) => item.group.traceId)).toEqual(["trace-early", "trace-a1"]);
  });

  it("keeps session ordering stable while an older request keeps streaming", () => {
    const olderStreaming = createGroup("trace-a1", "session-a", 10);
    olderStreaming.lastEventAt = 999;
    const newerIdle = createGroup("trace-b1", "session-b", 30);
    newerIdle.lastEventAt = 50;

    const sessions = buildSessionViewModels([olderStreaming, newerIdle], [], null);

    expect(sessions.map((session) => session.sessionKey)).toEqual(["session-b", "session-a"]);
  });

  it("keeps request ordering stable by trace start time", () => {
    const olderStreaming = createGroup("trace-a1", "session-a", 10);
    olderStreaming.lastEventAt = 999;
    const newerIdle = createGroup("trace-a2", "session-a", 20);
    newerIdle.lastEventAt = 50;

    const items = buildTraceListItems([olderStreaming, newerIdle], null, "session-a");

    expect(items.map((item) => item.group.traceId)).toEqual(["trace-a2", "trace-a1"]);
  });

  it("ignores traces without a session key when building the session rail", () => {
    const groups = [
      createGroup("trace-a1", "session-a", 10),
      createGroup("trace-b1", "session-b", 20),
      createGroup("trace-rejected", "", 30),
    ];

    const sessions = buildSessionViewModels(groups, [], null);

    expect(sessions.map((session) => session.sessionKey)).toEqual(["session-b", "session-a"]);
  });
});
