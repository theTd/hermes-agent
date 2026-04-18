import type { SessionViewModel } from "../../domain/monitor-view";

function fmtTime(ts: number | null | undefined): string {
  if (!ts) {
    return "—";
  }
  return new Date(ts * 1000).toLocaleTimeString();
}

export function SessionRailPanel({
  sessions,
  onSelectSession,
}: {
  sessions: SessionViewModel[];
  onSelectSession: (sessionKey: string) => void;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b border-border/20 px-4 py-3">
        <div className="text-[10px] font-compressed uppercase tracking-[0.3em] opacity-50">Sessions</div>
        <div className="mt-2 text-xs font-mono-ui opacity-60">
          {sessions.length} visible session{sessions.length === 1 ? "" : "s"}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <div className="space-y-2">
          {sessions.map((session) => (
            <button
              key={session.sessionKey}
              type="button"
              onClick={() => onSelectSession(session.sessionKey)}
              title={session.sessionKey}
              className={`block w-full rounded-2xl border px-3 py-3 text-left transition-colors ${
                session.selected
                  ? "border-primary/60 bg-card/80"
                  : "border-border/30 bg-card/20 hover:bg-card/40"
              }`}
            >
              <div className="break-all text-sm font-medium [overflow-wrap:anywhere]">
                {session.displayTitle}
              </div>
              <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[10px] font-mono-ui uppercase tracking-wider opacity-60">
                <span>{session.traceCount} requests</span>
                <span>{session.activeCount} active</span>
                <span>{session.errorCount} errors</span>
              </div>
              <div className="mt-2 text-[10px] font-mono-ui opacity-50">
                last_seen {fmtTime(session.lastSeen ?? session.session.last_seen)}
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
