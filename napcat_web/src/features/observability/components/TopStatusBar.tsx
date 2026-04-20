import { useState } from "react";
import { Link } from "react-router-dom";
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

interface TopStatusBarProps {
  controller: ObservabilityController;
  showLogout?: boolean;
  logoutLoading?: boolean;
  onLogout?: () => void | Promise<void>;
}

export function TopStatusBar({
  controller,
  showLogout = false,
  logoutLoading = false,
  onLogout,
}: TopStatusBarProps) {
  const { connection, stats } = controller.state;
  const [confirmingClear, setConfirmingClear] = useState(false);

  async function handleClearData() {
    if (!confirmingClear) {
      setConfirmingClear(true);
      return;
    }
    setConfirmingClear(false);
    await controller.clearAllData();
  }

  function cancelClearData() {
    setConfirmingClear(false);
  }

  return (
    <div className="border-b border-border/40 bg-card/30 px-3 py-2">
      <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-sm font-semibold uppercase tracking-wider">NapCat Observatory</h1>
          <Link to="/dashboard" className="text-[10px] font-mono-ui opacity-60 hover:opacity-100">
            dashboard
          </Link>
          <Badge variant={(connection.paused ? "warning" : connection.wsConnected ? "success" : "destructive") as any} className="text-[10px] px-1.5 py-0 h-4">
            {connection.paused ? "PAUSED" : connection.wsConnected ? "LIVE" : connection.reconnecting ? "RECONNECTING" : "OFFLINE"}
          </Badge>
          {!connection.wsConnected && !connection.reconnecting && (
            <button type="button" onClick={() => controller.reconnect()} className="text-xs font-mono-ui text-destructive hover:opacity-100">
              retry
            </button>
          )}
          <Badge variant={(connection.followLatest ? "success" : "outline") as any} className="text-[10px] px-1.5 py-0 h-4">
            {connection.followLatest ? "FOLLOW" : "MANUAL"}
          </Badge>
          {connection.paused && connection.unreadWhilePaused > 0 && (
            <Badge variant={"outline" as any} className="text-[10px] px-1.5 py-0 h-4">{connection.unreadWhilePaused} unread</Badge>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs font-mono-ui opacity-80">
          <span>10s: {controller.eventRate10s}</span>
          {stats && <span>emit:{stats.emit_count}</span>}
          {stats && <span className={stats.drop_count > 0 ? "text-destructive" : ""}>drop:{stats.drop_count}</span>}
          {stats && <span>q:{stats.queue_size}/{stats.queue_capacity}</span>}
          <button type="button" onClick={connection.paused ? controller.resumeStream : controller.pauseStream} className="hover:opacity-100">
            {connection.paused ? "resume" : "pause"}
          </button>
          <button type="button" onClick={() => controller.toggleFollowLatest()} className="hover:opacity-100">
            {connection.followLatest ? "unfollow" : "follow"}
          </button>
          {confirmingClear ? (
            <span className="inline-flex items-center gap-2">
              <span className="text-destructive font-mono-ui">clear?</span>
              <button type="button" onClick={() => void handleClearData()} className="text-destructive hover:opacity-100 font-mono-ui">
                yes
              </button>
              <button type="button" onClick={cancelClearData} className="hover:opacity-100 font-mono-ui">
                no
              </button>
            </span>
          ) : (
            <button type="button" onClick={() => void handleClearData()} className="text-destructive hover:opacity-100">
              clear
            </button>
          )}
          {controller.selectedGroup && (
            <button
              type="button"
              onClick={() => {
                const group = controller.selectedGroup;
                if (!group) return;
                const data = JSON.stringify(group, null, 2);
                const blob = new Blob([data], { type: "application/json" });
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = `trace-${group.traceId}.json`;
                a.click();
                URL.revokeObjectURL(url);
              }}
              className="hover:opacity-100"
            >
              export
            </button>
          )}
          {showLogout && onLogout && (
            <button
              type="button"
              onClick={() => void onLogout()}
              disabled={logoutLoading}
              className="hover:opacity-100 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {logoutLoading ? "out..." : "logout"}
            </button>
          )}
        </div>
      </div>

      <div className="mt-1 text-xs font-mono-ui opacity-60">
        {filterSummary(controller)}
      </div>
    </div>
  );
}
