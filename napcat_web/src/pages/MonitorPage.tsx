import { AlertsRail } from "@/features/observability/components/AlertsRail";
import { FilterBar } from "@/features/observability/components/FilterBar";
import { MonitorShell } from "@/features/observability/components/MonitorShell";
import { TopStatusBar } from "@/features/observability/components/TopStatusBar";
import { TraceDetailView } from "@/features/observability/components/TraceDetailView";
import { SessionRailPanel } from "@/features/observability/components/monitor/SessionRailPanel";
import { TraceListPane } from "@/features/observability/components/monitor/TraceListPane";
import { useMonitorPageState } from "@/features/observability/hooks/useMonitorPageState";
import { useEffect } from "react";

interface MonitorPageProps {
  showLogout?: boolean;
  logoutLoading?: boolean;
  onLogout?: () => void | Promise<void>;
}

export default function MonitorPage({
  showLogout = false,
  logoutLoading = false,
  onLogout,
}: MonitorPageProps) {
  const {
    controller,
    sessionViewModels,
    traceListItems,
    selectSession,
    setActiveSessionKey,
  } = useMonitorPageState();

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const target = event.target as HTMLElement;
      const isTyping = target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable;
      if (event.key === "/" && !isTyping) {
        event.preventDefault();
        document.getElementById("obs-search-input")?.focus();
      }
      if (event.key === "p" && !isTyping) {
        event.preventDefault();
        if (controller.state.connection.paused) {
          controller.resumeStream();
        } else {
          controller.pauseStream();
        }
      }
      if (event.key === "f" && !isTyping) {
        event.preventDefault();
        controller.toggleFollowLatest();
      }
      if ((event.key === "j" || event.key === "ArrowDown") && !isTyping) {
        event.preventDefault();
        const currentIndex = traceListItems.findIndex((item) => item.selected);
        const nextIndex = currentIndex + 1 < traceListItems.length ? currentIndex + 1 : currentIndex;
        const nextTrace = traceListItems[nextIndex];
        if (nextTrace) {
          controller.setSelectedTrace(nextTrace.group.traceId);
        }
      }
      if ((event.key === "k" || event.key === "ArrowUp") && !isTyping) {
        event.preventDefault();
        const currentIndex = traceListItems.findIndex((item) => item.selected);
        const prevIndex = currentIndex > 0 ? currentIndex - 1 : currentIndex;
        const prevTrace = traceListItems[prevIndex];
        if (prevTrace) {
          controller.setSelectedTrace(prevTrace.group.traceId);
        }
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [controller, traceListItems]);

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
        topBar={(
          <TopStatusBar
            controller={controller}
            showLogout={showLogout}
            logoutLoading={logoutLoading}
            onLogout={onLogout}
          />
        )}
        filterBar={<FilterBar controller={controller} />}
        leftRail={leftRail}
        requestPane={(
          <TraceListPane
            items={traceListItems}
            selectedTraceId={controller.state.ui.selectedTraceId}
            onSelectTrace={controller.setSelectedTrace}
          />
        )}
        detailPane={<TraceDetailView controller={controller} />}
      />
    </>
  );
}
