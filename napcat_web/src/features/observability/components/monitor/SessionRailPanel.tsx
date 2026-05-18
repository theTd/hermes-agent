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
      <div className="border-b border-border/20 px-3 py-1.5">
        <div className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">Sessions</div>
        <div className="text-xs font-mono-ui opacity-60">
          {sessions.length} visible session{sessions.length === 1 ? "" : "s"}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="divide-y divide-border/10">
          {sessions.map((session) => (
            <button
              key={session.sessionKey}
              type="button"
              onClick={() => onSelectSession(session.sessionKey)}
              title={session.sessionKey}
              className={`block w-full px-3 py-1.5 text-left transition-colors ${
                session.selected
                  ? "bg-primary/10"
                  : "hover:bg-card/40"
              }`}
            >
              <div className="break-all text-xs font-medium [overflow-wrap:anywhere]">
                {session.displayTitle}
              </div>
              <div className="mt-0.5 flex flex-wrap gap-x-2 gap-y-0 text-[10px] font-mono-ui uppercase tracking-wider opacity-60">
                <span>{session.traceCount} req</span>
                <span>{session.activeCount} act</span>
                <span>{session.errorCount} err</span>
              </div>
              <div className="text-[10px] font-mono-ui opacity-50">
                {fmtTime(session.lastSeen ?? session.session.last_seen)}
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
