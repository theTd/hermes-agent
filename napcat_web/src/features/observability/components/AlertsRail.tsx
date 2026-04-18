import { Badge } from "@/components/ui/badge";
import type { NapcatAlert } from "@/lib/napcat-api";

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString();
}

export function AlertsRail({
  alerts,
  onSelectAlert,
}: {
  alerts: NapcatAlert[];
  onSelectAlert: (alert: NapcatAlert) => void;
}) {
  return (
    <div className="space-y-2">
      <div className="text-[10px] font-compressed uppercase tracking-widest opacity-50">Alerts</div>
      {alerts.slice(0, 5).map((alert) => (
        <button key={alert.alert_id} type="button" onClick={() => onSelectAlert(alert)} className="block w-full rounded border border-border/30 bg-card/20 p-2 text-left">
          <div className="flex items-center gap-2">
            <Badge variant={(alert.severity === "error" ? "destructive" : "warning") as any}>{alert.severity}</Badge>
            <span className="text-[10px] font-mono-ui opacity-50">{fmtTime(alert.ts)}</span>
          </div>
          <div className="mt-1 text-xs opacity-80">{alert.title}</div>
        </button>
      ))}
    </div>
  );
}
