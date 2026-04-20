import { useParams, useNavigate } from "react-router-dom";
import { useEffect } from "react";
import { TraceDetailView } from "@/features/observability/components/TraceDetailView";
import { TracePageHeader } from "@/features/observability/components/detail/TracePageHeader";
import { useObservabilityController } from "@/features/observability/hooks/useObservabilityController";

export default function TraceDetailPage() {
  const { traceId } = useParams<{ traceId: string }>();
  const navigate = useNavigate();
  const controller = useObservabilityController({
    mode: "detail",
    traceId,
  });
  const group = controller.selectedGroup;

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        navigate("/");
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [navigate]);

  if (controller.state.error) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-sm font-mono-ui text-destructive">{controller.state.error}</p>
      </div>
    );
  }

  if (!group) {
    return (
      <div className="flex min-h-screen flex-col">
        <div className="flex flex-wrap items-center gap-3 border-b border-border/30 px-4 py-3">
          <span className="text-xs font-mono-ui opacity-60">← Back to console</span>
          <span className="text-lg font-semibold tracking-wide">Trace</span>
        </div>
        <div className="flex flex-1 flex-col items-center justify-center gap-4 p-8">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-border border-t-primary" />
          <p className="text-sm font-mono-ui opacity-40">Loading trace…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col">
      <TracePageHeader group={group} />
      <div className="min-h-0 flex-1">
        <TraceDetailView controller={controller} />
      </div>
    </div>
  );
}
