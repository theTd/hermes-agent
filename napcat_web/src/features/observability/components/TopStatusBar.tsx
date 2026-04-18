import { Badge } from "@/components/ui/badge";
import type { ObservabilityController } from "../types";

function filterSummary(controller: ObservabilityController): string {
  const { filters } = controller.state;
  const tokens = [
    filters.view !== "all" ? `view:${filters.view}` : "",
    filters.status ? `status:${filters.status}` : "",
    filters.eventType ? `event:${filters.eventType}` : "",
    filters.chatId ? `chat:${filters.chatId}` : "",
    filters.userId ? `user:${filters.userId}` : "",
    filters.search ? `search:${filters.search}` : "",
  ].filter(Boolean);
  return tokens.join(" · ") || "no filters";
}

export function TopStatusBar({ controller }: { controller: ObservabilityController }) {
  const { connection, stats } = controller.state;

  async function handleClearData() {
    const confirmed = window.confirm(
      "Clear all persisted observability data? This removes all traces, events, alerts, and runtime state from the observability store.",
    );
    if (!confirmed) {
      return;
    }
    await controller.clearAllData();
  }

  return (
    <div className="border-b border-border/40 bg-card/30 px-4 py-3">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="font-collapse text-xl tracking-wider uppercase">NapCat Observatory</h1>
          <Badge variant={(connection.paused ? "warning" : connection.wsConnected ? "success" : "destructive") as any}>
            {connection.paused ? "PAUSED" : connection.wsConnected ? "LIVE" : connection.reconnecting ? "RECONNECTING" : "OFFLINE"}
          </Badge>
          <Badge variant={(connection.followLatest ? "success" : "outline") as any}>
            {connection.followLatest ? "FOLLOW LATEST" : "MANUAL"}
          </Badge>
          {connection.paused && connection.unreadWhilePaused > 0 && (
            <Badge variant={"outline" as any}>{connection.unreadWhilePaused} unread</Badge>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-3 text-xs font-mono-ui opacity-80">
          <span>10s rate: {controller.eventRate10s}</span>
          {stats && <span>emit: {stats.emit_count}</span>}
          {stats && <span className={stats.drop_count > 0 ? "text-destructive" : ""}>drop: {stats.drop_count}</span>}
          {stats && <span>queue: {stats.queue_size}/{stats.queue_capacity}</span>}
          <button type="button" onClick={connection.paused ? controller.resumeStream : controller.pauseStream} className="hover:opacity-100">
            {connection.paused ? "resume" : "pause"}
          </button>
          <button type="button" onClick={() => controller.toggleFollowLatest()} className="hover:opacity-100">
            {connection.followLatest ? "disable follow" : "follow latest"}
          </button>
          <button type="button" onClick={() => void handleClearData()} className="text-destructive hover:opacity-100">
            clear data
          </button>
        </div>
      </div>

      <div className="mt-2 text-xs font-mono-ui opacity-60">
        {filterSummary(controller)}
      </div>
    </div>
  );
}
