/**
 * NapCat Observability API client.
 *
 * This frontend is served by the observability server itself,
 * so all API calls use relative URLs (same origin).
 */

export const OBS_AUTH_REQUIRED_EVENT = "napcat-observatory-auth-required";

interface FetchApiInit extends RequestInit {
  skipAuthEvent?: boolean;
}

async function fetchApi<T>(path: string, init?: FetchApiInit): Promise<T> {
  const { skipAuthEvent = false, ...requestInit } = init || {};
  const headers = new Headers(requestInit.headers);
  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const res = await fetch(path, {
    ...requestInit,
    headers,
    credentials: "same-origin",
  });
  if (!res.ok) {
    if ((res.status === 401 || res.status === 403) && !skipAuthEvent) {
      window.dispatchEvent(new CustomEvent(OBS_AUTH_REQUIRED_EVENT));
    }
    throw new Error(`${res.status}: ${await res.text()}`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface NapcatEvent {
  event_id: string;
  trace_id: string;
  span_id: string;
  parent_span_id: string;
  ts: number;
  session_key: string;
  session_id: string;
  platform: string;
  chat_type: string;
  chat_id: string;
  user_id: string;
  message_id: string;
  event_type: string;
  severity: string;
  payload: Record<string, any>;
}

export interface NapcatTrace {
  trace_id: string;
  session_key: string;
  session_id: string;
  chat_type: string;
  chat_id: string;
  user_id: string;
  message_id: string;
  started_at: number;
  ended_at: number | null;
  event_count: number;
  status: string;
  latest_stage?: string;
  active_stream?: boolean;
  tool_call_count?: number;
  error_count?: number;
  model?: string;
  summary: Record<string, any>;
  events?: NapcatEvent[];
}

export interface NapcatSession {
  session_key: string;
  session_id: string;
  trace_count: number;
  first_seen: number;
  last_seen: number;
  total_events: number;
}

export interface NapcatAlert {
  alert_id: string;
  trace_id: string;
  event_id: string;
  ts: number;
  severity: string;
  title: string;
  detail: string;
  acknowledged: number;
}

export interface RuntimeState {
  [key: string]: { value: any; updated_at: number };
}

export interface PublisherStats {
  enabled: boolean;
  emit_count: number;
  drop_count: number;
  queue_size: number;
  queue_capacity: number;
}

// ---------------------------------------------------------------------------
// Query helpers
// ---------------------------------------------------------------------------

interface EventFilters {
  trace_id?: string;
  time_start?: number;
  time_end?: number;
  session_key?: string;
  chat_id?: string;
  user_id?: string;
  message_id?: string;
  event_type?: string;
  severity?: string;
  search?: string;
  limit?: number;
  offset?: number;
}

export interface TraceFilters {
  time_start?: number;
  time_end?: number;
  session_key?: string;
  chat_id?: string;
  user_id?: string;
  status?: string;
  event_type?: string;
  search?: string;
  view?: string;
  limit?: number;
  offset?: number;
}

function toQuery(params: Record<string, any>): string {
  const entries = Object.entries(params).filter(([, v]) => v != null && v !== "");
  if (!entries.length) return "";
  return "?" + entries.map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
}

// ---------------------------------------------------------------------------
// REST API
// ---------------------------------------------------------------------------

export async function getHealth(): Promise<{ status: string; service: string; store: string }> {
  return fetchApi("/api/napcat/health");
}

export async function getRuntime(): Promise<RuntimeState> {
  return fetchApi("/api/napcat/runtime");
}

export async function getSessions(params?: { time_start?: number; time_end?: number; limit?: number; offset?: number }): Promise<{ sessions: NapcatSession[]; total: number }> {
  return fetchApi(`/api/napcat/sessions${toQuery(params || {})}`);
}

export async function getTraces(params?: TraceFilters): Promise<{ traces: NapcatTrace[]; total: number }> {
  return fetchApi(`/api/napcat/traces${toQuery(params || {})}`);
}

export async function getTrace(traceId: string): Promise<NapcatTrace> {
  return fetchApi(`/api/napcat/traces/${encodeURIComponent(traceId)}`);
}

export async function getTraceViewModels(params?: TraceFilters): Promise<{ groups: any[]; total: number }> {
  return fetchApi(`/api/napcat/traces/view_model${toQuery(params || {})}`);
}

export async function getTraceViewModel(traceId: string): Promise<any> {
  return fetchApi(`/api/napcat/traces/${encodeURIComponent(traceId)}/view_model`);
}

export async function getEvents(params?: EventFilters): Promise<{ events: NapcatEvent[]; total: number }> {
  return fetchApi(`/api/napcat/events${toQuery(params || {})}`);
}

export async function getMessage(messageId: string): Promise<{ message_id: string; events: NapcatEvent[]; traces: NapcatTrace[] }> {
  return fetchApi(`/api/napcat/messages/${encodeURIComponent(messageId)}`);
}

export async function getAlerts(params?: { acknowledged?: boolean; severity?: string; limit?: number; offset?: number }): Promise<{ alerts: NapcatAlert[]; total: number }> {
  return fetchApi(`/api/napcat/alerts${toQuery(params || {})}`);
}

export interface DashboardStats {
  time_window: { start: number; end: number };
  traces: {
    total: number;
    errors: number;
    active: number;
    error_rate: number;
  };
  performance: {
    avg_duration_ms: number;
  };
  models: Array<{ model: string; count: number }>;
  event_buckets: Array<{ minute: number; count: number }>;
  recent_errors: NapcatTrace[];
  error_types: Array<{ event_type: string; count: number }>;
}

export async function getDashboardStats(params?: { time_start?: number; time_end?: number }): Promise<DashboardStats> {
  return fetchApi(`/api/napcat/dashboard${toQuery(params || {})}`);
}

export async function getStats(): Promise<PublisherStats> {
  return fetchApi("/api/napcat/stats");
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

export async function reconnectAdapter(): Promise<{ status: string }> {
  return fetchApi("/api/napcat/actions/reconnect", { method: "POST" });
}

export async function cleanupData(retentionDays: number = 30): Promise<{ status: string; cleaned: Record<string, number> }> {
  return fetchApi("/api/napcat/actions/cleanup", {
    method: "POST",
    body: JSON.stringify({ retention_days: retentionDays }),
  });
}

export async function clearAllObservabilityData(): Promise<{ status: string; cleared: Record<string, number> }> {
  return fetchApi("/api/napcat/actions/clear-all", {
    method: "POST",
  });
}

export async function setDiagnosticLevel(level: string): Promise<{ status: string }> {
  return fetchApi("/api/napcat/actions/diagnostic-level", {
    method: "POST",
    body: JSON.stringify({ level }),
  });
}

export async function acknowledgeAlert(alertId: string): Promise<{ status: string }> {
  return fetchApi(`/api/napcat/actions/alerts/${encodeURIComponent(alertId)}/acknowledge`, { method: "POST" });
}

// ---------------------------------------------------------------------------
// WebSocket (same origin)
// ---------------------------------------------------------------------------

export function createObsWebSocket(): WebSocket {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return new WebSocket(`${proto}//${location.host}/ws/napcat/stream`);
}

// ---------------------------------------------------------------------------
// WebSocket message types
// ---------------------------------------------------------------------------

export interface WsSnapshotInitMessage {
  type: "snapshot.init";
  data: {
    groups?: any[];
    traces?: NapcatTrace[];
    sessions?: NapcatSession[];
    alerts?: NapcatAlert[];
    runtime?: RuntimeState;
    stats?: PublisherStats;
    cursor?: number | null;
  };
}

export interface WsEventAppendMessage {
  type: "event.append";
  data: NapcatEvent;
}

export interface WsTraceUpdateMessage {
  type: "trace.update";
  data: NapcatTrace;
}

export interface WsGroupUpdateMessage {
  type: "group.update";
  data: any;
}

export interface WsRuntimeUpdateMessage {
  type: "runtime.update";
  data: RuntimeState;
}

export interface WsAlertRaisedMessage {
  type: "alert.raised";
  data: NapcatAlert;
}

export interface WsBackfillCompleteMessage {
  type: "backfill.complete";
  data: {
    cursor: number | null;
    count: number;
  };
}

export interface WsDashboardUpdateMessage {
  type: "dashboard.update";
  data: DashboardStats;
}

export type WsIncomingMessage =
  | WsSnapshotInitMessage
  | WsEventAppendMessage
  | WsTraceUpdateMessage
  | WsGroupUpdateMessage
  | WsRuntimeUpdateMessage
  | WsAlertRaisedMessage
  | WsBackfillCompleteMessage
  | WsDashboardUpdateMessage;

export interface WsSubscriptionFilters {
  status?: string;
  event_type?: string;
  search?: string;
  session_key?: string;
  chat_id?: string;
  user_id?: string;
  view?: string;
  trace_id?: string;
}

// ---------------------------------------------------------------------------
// Access control
// ---------------------------------------------------------------------------

export interface ObsAuthStatus {
  enabled: boolean;
  authenticated: boolean;
}

export async function getObsAuthStatus(): Promise<ObsAuthStatus> {
  return fetchApi("/api/napcat/auth/status", { skipAuthEvent: true });
}

export async function loginObsAccessToken(token: string): Promise<{ status: string; authenticated: boolean }> {
  return fetchApi("/api/napcat/auth/login", {
    method: "POST",
    body: JSON.stringify({ token }),
    skipAuthEvent: true,
  });
}

export async function logoutObsAccess(): Promise<{ status: string; authenticated: boolean }> {
  return fetchApi("/api/napcat/auth/logout", {
    method: "POST",
    skipAuthEvent: true,
  });
}
