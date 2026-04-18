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
