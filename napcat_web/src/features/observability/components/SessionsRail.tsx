import type { NapcatSession } from "@/lib/napcat-api";
import { formatSessionLabel } from "../domain/session-label";

function fmtTime(ts: number | null | undefined): string {
  if (!ts) {
    return "—";
  }
  return new Date(ts * 1000).toLocaleTimeString();
}

export interface SessionRailStat {
  traceCount: number;
  activeCount: number;
  errorCount: number;
  lastSeen: number | null;
}

export function SessionsRail({
  sessions,
  selectedSessionKey,
  statsByKey,
  onSelectSession,
}: {
  sessions: NapcatSession[];
  selectedSessionKey: string | null;
  statsByKey: Record<string, SessionRailStat>;
  onSelectSession: (session: NapcatSession) => void;
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
          {sessions.map((session) => {
            const stats = statsByKey[session.session_key];
            const selected = selectedSessionKey === session.session_key;
            return (
              <button
                key={session.session_key}
                type="button"
                onClick={() => onSelectSession(session)}
                title={session.session_key}
                className={`block w-full rounded-xl border px-3 py-3 text-left transition-colors ${
                  selected
                    ? "border-primary/60 bg-card/80"
                    : "border-border/30 bg-card/20 hover:bg-card/40"
                }`}
              >
                <div className="break-all text-sm font-medium [overflow-wrap:anywhere]">
                  {formatSessionLabel(session.session_key)}
                </div>
                <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[10px] font-mono-ui uppercase tracking-wider opacity-60">
                  <span>{stats?.traceCount ?? session.trace_count} requests</span>
                  <span>{stats?.activeCount ?? 0} active</span>
                  <span>{stats?.errorCount ?? 0} errors</span>
                </div>
                <div className="mt-2 text-[10px] font-mono-ui opacity-50">
                  last_seen {fmtTime(stats?.lastSeen ?? session.last_seen)}
                </div>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
