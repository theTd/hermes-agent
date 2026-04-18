import { AlertsRail } from "@/features/observability/components/AlertsRail";
import { FilterBar } from "@/features/observability/components/FilterBar";
import { MonitorShell } from "@/features/observability/components/MonitorShell";
import { TopStatusBar } from "@/features/observability/components/TopStatusBar";
import { TraceDetailView } from "@/features/observability/components/TraceDetailView";
import { SessionRailPanel } from "@/features/observability/components/monitor/SessionRailPanel";
import { TraceListPane } from "@/features/observability/components/monitor/TraceListPane";
import { useMonitorPageState } from "@/features/observability/hooks/useMonitorPageState";

export default function MonitorPage() {
  const {
    controller,
    sessionViewModels,
    traceListItems,
    selectSession,
    setActiveSessionKey,
  } = useMonitorPageState();

  const leftRail = (
    <div className="grid h-full min-h-0 grid-rows-[minmax(0,1fr)_auto]">
      <SessionRailPanel
        sessions={sessionViewModels}
        onSelectSession={selectSession}
      />
      <div className="border-t border-border/20 p-4">
        <AlertsRail
          alerts={controller.alerts}
          onSelectAlert={(alert) => {
            const target = controller.groups.find((group) => group.traceId === alert.trace_id);
            if (target) {
              setActiveSessionKey(target.trace.session_key);
            }
            controller.setSelectedTrace(alert.trace_id);
            controller.setActiveInspectorSection("final");
          }}
        />
      </div>
    </div>
  );

  return (
    <>
      {controller.state.error && (
        <div className="border-b border-destructive/30 bg-destructive/10 px-4 py-2 text-xs font-mono-ui text-destructive">
          {controller.state.error}
        </div>
      )}
      <MonitorShell
        topBar={<TopStatusBar controller={controller} />}
        filterBar={<FilterBar controller={controller} />}
        leftRail={leftRail}
        requestPane={(
          <TraceListPane
            items={traceListItems}
            onSelectTrace={controller.setSelectedTrace}
          />
        )}
        detailPane={<TraceDetailView controller={controller} />}
      />
    </>
  );
}
