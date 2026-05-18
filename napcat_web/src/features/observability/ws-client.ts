import { createObsWebSocket } from "@/lib/napcat-api";
import type { WsIncomingMessage, WsSubscriptionFilters } from "@/lib/napcat-api";

interface ConnectionStateUpdate {
  wsConnected: boolean;
  reconnecting: boolean;
}

interface ObservabilityWsClientOptions {
  onMessage: (message: WsIncomingMessage) => void;
  onConnectionStateChange?: (state: ConnectionStateUpdate) => void;
  reconnectDelayMs?: number;
}

const LATENCY_WARN_THRESHOLD_MS = 2000;
const LATENCY_DEBUG_STORAGE_KEY = "napcatObservatoryLatencyDebug";

function finiteNumber(value: unknown): number | null {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function logLatencyBreakdown(message: WsIncomingMessage): void {
  if (message.type !== "event.append") {
    return;
  }

  const event = message.data;
  if (event.event_type !== "trace.created" && event.event_type !== "message.received") {
    return;
  }

  const payload = event.payload as Record<string, unknown>;
  const timing = payload?.timing;
  if (!timing || typeof timing !== "object") {
    return;
  }

  const browserReceivedAt = Date.now() / 1000;
  (timing as Record<string, unknown>).browser_received_at = browserReceivedAt;

  const adapterReceivedAt = finiteNumber((timing as Record<string, unknown>).adapter_received_at);
  const emittedAt = finiteNumber((timing as Record<string, unknown>).observability_emitted_at) ?? finiteNumber(event.ts);
  const wsScheduledAt = finiteNumber((timing as Record<string, unknown>).ws_broadcast_scheduled_at);
  const wsStartedAt = finiteNumber((timing as Record<string, unknown>).ws_broadcast_started_at);
  if (adapterReceivedAt == null || emittedAt == null) {
    return;
  }

  const totalMs = (browserReceivedAt - adapterReceivedAt) * 1000;
  if (!Number.isFinite(totalMs) || totalMs < 0 || totalMs > 30000) {
    return;
  }

  const breakdown = {
    eventType: event.event_type,
    traceId: event.trace_id,
    messageId: event.message_id,
    totalMs: Math.round(totalMs),
    adapterToEmitMs: Math.round((emittedAt - adapterReceivedAt) * 1000),
    emitToWsScheduleMs: wsScheduledAt == null ? null : Math.round((wsScheduledAt - emittedAt) * 1000),
    wsScheduleToWsStartMs: wsStartedAt == null || wsScheduledAt == null ? null : Math.round((wsStartedAt - wsScheduledAt) * 1000),
    wsStartToBrowserMs: wsStartedAt == null ? null : Math.round((browserReceivedAt - wsStartedAt) * 1000),
    textPreview: String(payload?.text_preview || "").slice(0, 120),
  };

  const verbose = globalThis.localStorage?.getItem(LATENCY_DEBUG_STORAGE_KEY) === "1";
  if (breakdown.totalMs >= LATENCY_WARN_THRESHOLD_MS) {
    console.warn("[NapCat Observatory latency]", breakdown);
    return;
  }
  if (verbose) {
    console.debug("[NapCat Observatory latency]", breakdown);
  }
}

function isIncomingMessage(value: unknown): value is WsIncomingMessage {
  if (!value || typeof value !== "object") {
    return false;
  }

  const message = value as { type?: unknown; data?: unknown };
  if (typeof message.type !== "string") {
    return false;
  }

  switch (message.type) {
    case "snapshot.init":
    case "runtime.update":
    case "event.append":
    case "trace.update":
    case "alert.raised":
    case "backfill.complete":
    case "dashboard.update":
      return message.data != null;
    default:
      return false;
  }
}

export class ObservabilityWsClient {
  private socket: WebSocket | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private closedByUser = false;
  private readonly reconnectDelayMs: number;
  private currentFilters: WsSubscriptionFilters = {};
  private readonly options: ObservabilityWsClientOptions;

  constructor(options: ObservabilityWsClientOptions) {
    this.options = options;
    this.reconnectDelayMs = options.reconnectDelayMs ?? 3000;
  }

  connect(): void {
    this.closedByUser = false;
    this.openSocket();
  }

  disconnect(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.socket) {
      this.socket.onopen = null;
      this.socket.onclose = null;
      this.socket.onerror = null;
      this.socket.onmessage = null;
      this.socket.close();
    }
    this.socket = null;
    this.options.onConnectionStateChange?.({
      wsConnected: false,
      reconnecting: false,
    });
  }

  pause(): void {
    this.send({ type: "pause" });
  }

  resume(): void {
    this.send({ type: "resume" });
  }

  backfill(cursor: number, filters?: WsSubscriptionFilters): void {
    this.send({
      type: "backfill",
      cursor,
      filters: filters ?? this.currentFilters,
    });
  }

  subscribe(filters: WsSubscriptionFilters): void {
    this.currentFilters = filters;
    this.send({
      type: "subscribe",
      filters,
    });
  }

  private openSocket(): void {
    if (
      this.socket &&
      (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }

    const socket = createObsWebSocket();
    this.socket = socket;

    socket.onopen = () => {
      if (Object.keys(this.currentFilters).length > 0) {
        this.subscribe(this.currentFilters);
      }
      this.options.onConnectionStateChange?.({
        wsConnected: true,
        reconnecting: false,
      });
    };

    socket.onclose = () => {
      if (this.socket !== socket) {
        return;
      }
      this.socket = null;
      const reconnecting = !this.closedByUser;
      this.options.onConnectionStateChange?.({
        wsConnected: false,
        reconnecting,
      });
      if (!reconnecting) {
        return;
      }
      this.reconnectTimer = setTimeout(() => this.openSocket(), this.reconnectDelayMs);
    };

    socket.onerror = () => {
      if (this.socket !== socket) {
        return;
      }
      this.options.onConnectionStateChange?.({
        wsConnected: false,
        reconnecting: !this.closedByUser,
      });
    };

    socket.onmessage = (event) => {
      try {
        const parsed = JSON.parse(event.data) as unknown;
        if (isIncomingMessage(parsed)) {
          logLatencyBreakdown(parsed);
          this.options.onMessage(parsed);
        }
      } catch {
        // Ignore malformed frames.
      }
    };
  }

  private send(payload: Record<string, unknown>): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      return;
    }
    this.socket.send(JSON.stringify(payload));
  }
}
