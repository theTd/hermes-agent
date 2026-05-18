import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { createObsWebSocket, getDashboardStats, type DashboardStats } from "@/lib/napcat-api";
import { KpiCard } from "@/features/observability/components/dashboard/KpiCard";
import { Sparkline } from "@/features/observability/components/dashboard/Sparkline";
import { ModelDistribution } from "@/features/observability/components/dashboard/ModelDistribution";
import { RecentErrorsTable } from "@/features/observability/components/dashboard/RecentErrorsTable";

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export default function DashboardPage() {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const wsConnectedRef = useRef(false);

  const fetchStats = useCallback(async () => {
    try {
      const data = await getDashboardStats();
      setStats(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load dashboard");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchStats();
    // Slower polling as fallback when WS is offline.
    const interval = setInterval(fetchStats, 30000);
    return () => clearInterval(interval);
  }, [fetchStats]);

  // Real-time dashboard updates via WebSocket.
  useEffect(() => {
    const ws = createObsWebSocket();
    ws.onopen = () => {
      wsConnectedRef.current = true;
      ws.send(JSON.stringify({ type: "subscribe" }));
    };
    ws.onclose = () => {
      wsConnectedRef.current = false;
    };
    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "dashboard.update" && msg.data) {
          setStats(msg.data);
          setError(null);
        }
      } catch {
        // Ignore malformed frames.
      }
    };
    ws.onerror = () => {
      wsConnectedRef.current = false;
    };
    return () => {
      ws.close();
    };
  }, []);

  if (loading && !stats) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-border border-t-primary" />
      </div>
    );
  }

  if (error && !stats) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-sm font-mono-ui text-destructive">{error}</p>
      </div>
    );
  }

  if (!stats) return null;

  const eventCounts = stats.event_buckets.map((b) => b.count);
  const totalMinutes = Math.max(
    10,
    stats.event_buckets.length > 0
      ? stats.event_buckets[stats.event_buckets.length - 1].minute + 1
      : 10
  );
  const paddedCounts = Array.from({ length: totalMinutes }, (_, i) => {
    const found = stats.event_buckets.find((b) => b.minute === i);
    return found ? found.count : 0;
  });

  return (
    <div className="min-h-screen">
      <div className="border-b border-border/40 bg-card/30 px-3 py-2">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-sm font-semibold uppercase tracking-wider">Dashboard</h1>
          <Link to="/" className="text-xs font-mono-ui opacity-60 hover:opacity-100">
            ← Monitor
          </Link>
        </div>
      </div>

      <div className="space-y-3 p-3">
        {/* KPI Row */}
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <KpiCard
            title="Total Traces"
            value={String(stats.traces.total)}
            subtext={`${stats.traces.active} active`}
          />
          <KpiCard
            title="Error Rate"
            value={`${(stats.traces.error_rate * 100).toFixed(1)}%`}
            subtext={`${stats.traces.errors} errors`}
            variant={stats.traces.error_rate > 0.05 ? "error" : "default"}
          />
          <KpiCard
            title="Avg Duration"
            value={fmtDuration(stats.performance.avg_duration_ms)}
          />
          <KpiCard
            title="Events (10m)"
            value={String(eventCounts.reduce((a, b) => a + b, 0))}
          />
        </div>

        {/* Charts Row */}
        <div className="grid gap-2 lg:grid-cols-[1fr_280px]">
          <div className="rounded bg-background/20 p-3">
            <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/60">
              Event Rate (per minute)
            </div>
            <div className="mt-2 overflow-x-auto">
              <Sparkline data={paddedCounts} width={600} height={80} />
            </div>
          </div>
          <ModelDistribution models={stats.models} />
        </div>

        {/* Errors + Error Types */}
        <div className="grid gap-2 lg:grid-cols-[1fr_280px]">
          <RecentErrorsTable traces={stats.recent_errors} />
          <div className="rounded bg-background/20 p-3">
            <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/60">
              Error Types
            </div>
            <div className="mt-2 divide-y divide-border/10">
              {stats.error_types.map((et) => (
                <div key={et.event_type} className="flex items-center justify-between py-1.5">
                  <span className="truncate text-xs font-mono-ui">{et.event_type}</span>
                  <span className="w-8 shrink-0 text-right text-[10px] font-mono-ui opacity-70">
                    {et.count}
                  </span>
                </div>
              ))}
              {stats.error_types.length === 0 && (
                <div className="py-2 text-xs font-mono-ui opacity-40">no errors</div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
