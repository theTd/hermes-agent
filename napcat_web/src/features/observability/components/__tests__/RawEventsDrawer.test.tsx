import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { RawEventsDrawer } from "../RawEventsDrawer";
import type { ObservabilityController } from "../../types";

function buildController(): ObservabilityController {
  return {
    state: {
      connection: {
        wsConnected: true,
        paused: false,
        followLatest: false,
        reconnecting: false,
        latestCursor: 0,
        unreadWhilePaused: 0,
      },
      filters: {
        status: "",
        eventType: "",
        search: "",
        sessionKey: "",
        chatId: "",
        userId: "",
        view: "all",
      },
      entities: {
        tracesById: {},
        eventsByTraceId: {},
        alertsById: {},
        sessionsByKey: {},
        groupsById: {},
      },
      runtime: {},
      stats: null,
      dashboardStats: null,
      ui: {
        selectedTraceId: null,
        expandedTraceIds: {},
        expandedRawEventIds: {},
        activeInspectorSection: null,
      },
      bootstrapped: true,
      error: null,
    },
    traces: [],
    groups: [],
    sessions: [],
    alerts: [],
    selectedTrace: null,
    selectedTraceEvents: [],
    selectedGroup: null,
    eventRate10s: 0,
    dashboardStats: null,
    setFilters: () => {},
    setSelectedTrace: () => {},
    toggleTraceExpanded: () => {},
    toggleRawEventExpanded: () => {},
    toggleFollowLatest: () => {},
    pauseStream: () => {},
    resumeStream: () => {},
    setActiveInspectorSection: () => {},
    reconnect: () => {},
    clearFilters: () => {},
    clearAllData: async () => {},
  };
}

describe("RawEventsDrawer", () => {
  it("renders compact payloads without slicing them", () => {
    const payload = {
      action: "reply",
      payload: {
        text: "A".repeat(320),
      },
    };
    const html = renderToStaticMarkup(
      <RawEventsDrawer
        controller={buildController()}
        events={[{
          event_id: "evt-1",
          trace_id: "trace-1",
          span_id: "span-1",
          parent_span_id: "",
          ts: 1,
          session_key: "session-1",
          session_id: "sess-1",
          platform: "napcat",
          chat_type: "group",
          chat_id: "chat-1",
          user_id: "user-1",
          message_id: "msg-1",
          event_type: "orchestrator.decision.parsed",
          severity: "info",
          payload,
        }]}
      />,
    );

    expect(html).toContain("A".repeat(320));
    expect(html).not.toContain("…");
  });
});
