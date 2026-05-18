import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import type { NapcatAlert } from "@/lib/napcat-api";

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString();
}

const DEFAULT_ALERT_LIMIT = 5;

export function AlertsRail({
  alerts,
  onSelectAlert,
}: {
  alerts: NapcatAlert[];
  onSelectAlert: (alert: NapcatAlert) => void;
}) {
  const [limit, setLimit] = useState(DEFAULT_ALERT_LIMIT);
  const visible = alerts.slice(0, limit);

  return (
    <div className="space-y-1">
      <div className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">Alerts</div>
      {visible.map((alert) => (
        <button key={alert.alert_id} type="button" onClick={() => onSelectAlert(alert)} className="block w-full border-b border-border/10 px-2 py-1.5 text-left last:border-b-0 hover:bg-card/30">
          <div className="flex items-center gap-2">
            <Badge variant={(alert.severity === "error" ? "destructive" : "warning") as any} className="text-[10px] px-1.5 py-0 h-4">{alert.severity}</Badge>
            <span className="text-[10px] font-mono-ui opacity-50">{fmtTime(alert.ts)}</span>
          </div>
          <div className="mt-0.5 text-xs opacity-80 truncate">{alert.title}</div>
        </button>
      ))}
      {alerts.length > visible.length && (
        <button
          type="button"
          onClick={() => setLimit((prev) => prev + DEFAULT_ALERT_LIMIT)}
          className="w-full text-left px-2 text-[10px] font-mono-ui opacity-50 hover:opacity-80"
        >
          + {alerts.length - visible.length} more
        </button>
      )}
    </div>
  );
}
