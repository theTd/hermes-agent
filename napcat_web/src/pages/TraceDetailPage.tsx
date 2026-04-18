import { useParams } from "react-router-dom";
import { TraceDetailView } from "@/features/observability/components/TraceDetailView";
import { TracePageHeader } from "@/features/observability/components/detail/TracePageHeader";
import { useObservabilityController } from "@/features/observability/hooks/useObservabilityController";

export default function TraceDetailPage() {
  const { traceId } = useParams<{ traceId: string }>();
  const controller = useObservabilityController({
    mode: "detail",
    traceId,
  });
  const group = controller.selectedGroup;

  if (controller.state.error) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-sm font-mono-ui text-destructive">{controller.state.error}</p>
      </div>
    );
  }

  if (!group) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-sm font-mono-ui opacity-40">Loading trace…</p>
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
