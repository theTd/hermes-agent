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
      <div className="border-b border-border/20 px-3 py-1.5">
        <div className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">Sessions</div>
        <div className="text-xs font-mono-ui opacity-60">
          {sessions.length} visible session{sessions.length === 1 ? "" : "s"}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="divide-y divide-border/10">
          {sessions.map((session) => {
            const stats = statsByKey[session.session_key];
            const selected = selectedSessionKey === session.session_key;
            return (
              <button
                key={session.session_key}
                type="button"
                onClick={() => onSelectSession(session)}
                title={session.session_key}
                className={`block w-full px-3 py-1.5 text-left transition-colors ${
                  selected
                    ? "bg-primary/10"
                    : "hover:bg-card/40"
                }`}
              >
                <div className="break-all text-xs font-medium [overflow-wrap:anywhere]">
                  {formatSessionLabel(session.session_key)}
                </div>
                <div className="mt-0.5 flex flex-wrap gap-x-2 gap-y-0 text-[10px] font-mono-ui uppercase tracking-wider opacity-60">
                  <span>{stats?.traceCount ?? session.trace_count} req</span>
                  <span>{stats?.activeCount ?? 0} act</span>
                  <span>{stats?.errorCount ?? 0} err</span>
                </div>
                <div className="text-[10px] font-mono-ui opacity-50">
                  {fmtTime(stats?.lastSeen ?? session.last_seen)}
                </div>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
