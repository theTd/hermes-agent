import type { NapcatSession } from "@/lib/napcat-api";
import type { LiveTraceGroup } from "../types";
import { formatSessionLabel } from "./session-label";

export interface SessionViewModel {
  sessionKey: string;
  displayTitle: string;
  session: NapcatSession;
  traceCount: number;
  activeCount: number;
  errorCount: number;
  lastSeen: number;
  selected: boolean;
}

interface SessionViewModelInternal extends SessionViewModel {
  sortStartedAt: number;
}

export interface TraceLanePreviewViewModel {
  key: string;
  title: string;
  model: string;
  requestCount: number;
  lastSummary: string;
}

export interface TraceListItemViewModel {
  group: LiveTraceGroup;
  selected: boolean;
  lanePreviews: TraceLanePreviewViewModel[];
}

function fallbackSessionFromTrace(group: LiveTraceGroup): NapcatSession {
  return {
    session_key: group.trace.session_key,
    session_id: group.trace.session_id,
    trace_count: 1,
    first_seen: group.startedAt,
    last_seen: group.lastEventAt,
    total_events: group.eventCount,
  };
}

function resolveSessionChatName(group: LiveTraceGroup): string {
  const summaryChatName = String(
    group.trace.summary?.chat_name
    || group.trace.summary?.group_name
    || "",
  ).trim();
  if (summaryChatName) {
    return summaryChatName;
  }

  for (const event of group.rawEvents) {
    const payload = event.payload || {};
    const eventChatName = String(payload.chat_name || payload.group_name || "").trim();
    if (eventChatName) {
      return eventChatName;
    }
  }

  return "";
}

export function buildSessionViewModels(
  groups: LiveTraceGroup[],
  sessions: NapcatSession[],
  selectedSessionKey: string | null,
): SessionViewModel[] {
  const statsByKey = new Map<string, SessionViewModelInternal>();
  const sessionsByKey = new Map(sessions.map((session) => [session.session_key, session]));

  for (const group of groups) {
    if (!group.trace.session_key) {
      continue;
    }
    const existing = statsByKey.get(group.trace.session_key);
    if (existing) {
      existing.traceCount += 1;
      existing.sortStartedAt = Math.max(existing.sortStartedAt, group.startedAt);
      existing.lastSeen = Math.max(existing.lastSeen, group.lastEventAt);
      if (group.status === "in_progress") {
        existing.activeCount += 1;
      }
      if (group.status === "error") {
        existing.errorCount += 1;
      }
      continue;
    }

    statsByKey.set(group.trace.session_key, {
      sessionKey: group.trace.session_key,
      displayTitle: formatSessionLabel(group.trace.session_key, {
        chatType: group.trace.chat_type,
        chatId: group.trace.chat_id,
        chatName: resolveSessionChatName(group),
      }),
      session: sessionsByKey.get(group.trace.session_key) ?? fallbackSessionFromTrace(group),
      traceCount: 1,
      activeCount: group.status === "in_progress" ? 1 : 0,
      errorCount: group.status === "error" ? 1 : 0,
      lastSeen: group.lastEventAt,
      selected: selectedSessionKey === group.trace.session_key,
      sortStartedAt: group.startedAt,
    });
  }

  return [...statsByKey.values()]
    .sort((left, right) => {
      if (left.sortStartedAt !== right.sortStartedAt) {
        return right.sortStartedAt - left.sortStartedAt;
      }
      if (left.lastSeen !== right.lastSeen) {
        return right.lastSeen - left.lastSeen;
      }
      return left.sessionKey.localeCompare(right.sessionKey);
    })
    .map(({ sortStartedAt: _sortStartedAt, ...session }) => ({
      ...session,
      selected: selectedSessionKey === session.sessionKey,
    }));
}

export function buildTraceListItems(
  groups: LiveTraceGroup[],
  selectedTraceId: string | null,
  sessionKey: string | null,
): TraceListItemViewModel[] {
  return groups
    .filter((group) => !sessionKey || !group.trace.session_key || group.trace.session_key === sessionKey)
    .sort((left, right) => {
      if (left.startedAt !== right.startedAt) {
        return right.startedAt - left.startedAt;
      }
      return left.traceId.localeCompare(right.traceId);
    })
    .map((group) => ({
      group,
      selected: group.traceId === selectedTraceId,
      lanePreviews: group.timeline.lanes.slice(0, 3).map((lane) => ({
        key: lane.key,
        title: lane.title,
        model: lane.model,
        requestCount: lane.requestBlocks.length,
        lastSummary: lane.timelineEntries[lane.timelineEntries.length - 1]?.summary || "No lane activity yet.",
      })),
    }));
}

export function pickPreferredTraceForSession(
  groups: LiveTraceGroup[],
  sessionKey: string,
): LiveTraceGroup | null {
  const sessionGroups = groups.filter((group) => group.trace.session_key === sessionKey);
  return sessionGroups[0] ?? null;
}
