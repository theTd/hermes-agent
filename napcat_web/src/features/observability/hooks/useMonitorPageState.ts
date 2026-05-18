import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { buildSessionViewModels, buildTraceListItems, pickPreferredTraceForSession } from "../domain/monitor-view";
import type { FilterState } from "../types";
import { useObservabilityController } from "./useObservabilityController";

function filtersFromSearchParams(searchParams: URLSearchParams): Partial<FilterState> {
  return {
    view: (searchParams.get("view") as FilterState["view"] | null) ?? "all",
    status: (searchParams.get("status") as FilterState["status"] | null) ?? "",
    eventType: searchParams.get("event_type") ?? "",
    search: searchParams.get("search") ?? "",
    chatId: searchParams.get("chat") ?? "",
    userId: searchParams.get("user") ?? "",
  };
}

export function useMonitorPageState() {
  const [searchParams, setSearchParams] = useSearchParams();
  const initialFiltersRef = useRef(filtersFromSearchParams(searchParams));
  const initialTraceIdRef = useRef(searchParams.get("trace"));
  const initialFollowRef = useRef(searchParams.get("follow"));
  const initialSessionRef = useRef(searchParams.get("session"));
  const controller = useObservabilityController({
    initialFilters: initialFiltersRef.current,
  });
  const [activeSessionKey, setActiveSessionKey] = useState<string | null>(initialSessionRef.current);
  const searchParamString = searchParams.toString();
  const { filters, ui } = controller.state;
  const selectedTraceId = ui.selectedTraceId;

  useEffect(() => {
    const traceId = initialTraceIdRef.current;
    if ((traceId ?? null) !== selectedTraceId) {
      controller.setSelectedTrace(traceId);
    }
    const followParam = initialFollowRef.current;
    const shouldFollowLatest = followParam === "1"
      ? true
      : followParam === "0"
        ? false
        : null;
    if (shouldFollowLatest !== null) {
      controller.toggleFollowLatest(shouldFollowLatest);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (controller.state.connection.followLatest && controller.selectedTrace) {
      setActiveSessionKey(controller.selectedTrace.session_key);
      return;
    }
    if (!activeSessionKey && controller.selectedGroup) {
      setActiveSessionKey(controller.selectedGroup.trace.session_key);
      return;
    }
    if (
      activeSessionKey
      && controller.sessions.length > 0
      && !controller.sessions.some((session) => session.session_key === activeSessionKey)
    ) {
      setActiveSessionKey(controller.sessions[0]?.session_key ?? null);
    }
  }, [
    activeSessionKey,
    controller.selectedGroup,
    controller.selectedTrace,
    controller.sessions,
    controller.state.connection.followLatest,
  ]);

  useEffect(() => {
    const next = new URLSearchParams(searchParams);
    if (filters.view !== "all") next.set("view", filters.view);
    else next.delete("view");
    if (filters.status) next.set("status", filters.status);
    else next.delete("status");
    if (filters.eventType) next.set("event_type", filters.eventType);
    else next.delete("event_type");
    if (filters.search) next.set("search", filters.search);
    else next.delete("search");
    if (filters.chatId) next.set("chat", filters.chatId);
    else next.delete("chat");
    if (filters.userId) next.set("user", filters.userId);
    else next.delete("user");
    if (selectedTraceId) next.set("trace", selectedTraceId);
    else next.delete("trace");
    if (activeSessionKey) next.set("session", activeSessionKey);
    else next.delete("session");
    if (controller.state.connection.followLatest) next.set("follow", "1");
    else next.delete("follow");
    if (next.toString() !== searchParamString) {
      setSearchParams(next, { replace: true });
    }
  }, [
    activeSessionKey,
    controller.state.connection.followLatest,
    filters.chatId,
    filters.eventType,
    filters.search,
    filters.status,
    filters.userId,
    filters.view,
    searchParamString,
    selectedTraceId,
    setSearchParams,
  ]);

  const sessionViewModels = useMemo(
    () => buildSessionViewModels(controller.groups, controller.sessions, activeSessionKey),
    [activeSessionKey, controller.groups, controller.sessions],
  );
  const traceListItems = useMemo(
    () => buildTraceListItems(controller.groups, controller.selectedGroup?.traceId ?? controller.state.ui.selectedTraceId, activeSessionKey),
    [activeSessionKey, controller.groups, controller.selectedGroup?.traceId, controller.state.ui.selectedTraceId],
  );

  return {
    controller,
    activeSessionKey,
    setActiveSessionKey,
    sessionViewModels,
    traceListItems,
    selectSession: (sessionKey: string) => {
      setActiveSessionKey(sessionKey);
      const preferredTrace = pickPreferredTraceForSession(controller.groups, sessionKey);
      if (preferredTrace) {
        controller.setSelectedTrace(preferredTrace.traceId);
      }
    },
  };
}
