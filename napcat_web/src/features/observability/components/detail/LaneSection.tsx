import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { InspectorSection } from "../InspectorSection";
import type { TraceDetailLaneViewModel } from "../../domain/detail-view";
import { RequestBlockCard } from "./RequestBlockCard";
import { TimelineEventCard } from "./TimelineEventCard";

function useFlashOnHashMatch(laneKey: string) {
  const [flashing, setFlashing] = useState(false);
  useEffect(() => {
    function check() {
      if (window.location.hash === `#lane-${laneKey}`) {
        setFlashing(true);
        const t = setTimeout(() => setFlashing(false), 1200);
        return () => clearTimeout(t);
      }
    }
    check();
    window.addEventListener("hashchange", check);
    return () => window.removeEventListener("hashchange", check);
  }, [laneKey]);
  return flashing;
}

function MainOrchestratorReasoningPanel({
  content,
  onOpenFullscreen,
}: {
  content: string;
  onOpenFullscreen: (block: { label: string; content: string; title?: string }) => void;
}) {
  if (!content.trim()) {
    return null;
  }

  return (
    <div
      className="rounded bg-background/20 p-2"
      data-main-orchestrator-thinking="true"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/70">
            Main Orchestrator Reasoning
          </div>
        </div>
        <button
          type="button"
          className="text-[10px] font-mono-ui opacity-70 transition-opacity hover:opacity-100"
          onClick={() => onOpenFullscreen({ label: "main_orchestrator.reasoning", content, title: "Main Orchestrator Reasoning" })}
        >
          fullscreen
        </button>
      </div>
      <pre className="mt-1 max-w-full whitespace-pre-wrap break-all rounded bg-background/30 p-2 text-xs font-mono-ui [overflow-wrap:anywhere]">
        {content}
      </pre>
    </div>
  );
}

export function LaneSection({
  lane,
  onOpenFullscreen,
  resetKey,
}: {
  lane: TraceDetailLaneViewModel;
  onOpenFullscreen: (block: { label: string; content: string; title?: string }) => void;
  resetKey: string;
}) {
  const flashing = useFlashOnHashMatch(lane.key);
  return (
    <div
      id={`lane-${lane.key}`}
      className={`rounded transition-colors duration-300 ${flashing ? "bg-primary/10" : ""}`}
    >
      <InspectorSection
        title={lane.title}
        forceExpanded={lane.key === "main_orchestrator"}
        resetKey={resetKey}
      >
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant={(lane.status === "error" ? "destructive" : lane.status === "completed" ? "success" : lane.status === "ignored" ? "warning" : "outline") as any} className="text-[10px] px-1.5 py-0 h-4">
            {lane.status}
          </Badge>
          {lane.model && <Badge variant={"outline" as any} className="text-[10px] px-1.5 py-0 h-4">{lane.model}</Badge>}
          {lane.provider && <Badge variant={"outline" as any} className="text-[10px] px-1.5 py-0 h-4">{lane.provider}</Badge>}
          <span className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">
            {lane.requestCount} request{lane.requestCount === 1 ? "" : "s"}
          </span>
        </div>

        <MainOrchestratorReasoningPanel
          content={lane.mainOrchestratorThinking}
          onOpenFullscreen={onOpenFullscreen}
        />

        {lane.timelineItems.length === 0 ? (
          <p className="text-xs font-mono-ui opacity-40">No timeline entries for this lane.</p>
        ) : (
          <div>
            {lane.timelineItems.map((item) => (
              item.kind === "request" && item.block
                ? (
                  <RequestBlockCard
                    key={item.id}
                    block={item.block}
                    onOpenFullscreen={onOpenFullscreen}
                    hideThinkingPreview={lane.key === "main_orchestrator" && Boolean(lane.mainOrchestratorThinking.trim())}
                    hideThinkingContent={lane.key === "main_orchestrator" && Boolean(lane.mainOrchestratorThinking.trim())}
                    laneModel={lane.model}
                    laneProvider={lane.provider}
                    laneRequestCount={lane.requestCount}
                  />
                )
                : item.entry
                  ? <TimelineEventCard key={item.id} entry={item.entry} onOpenFullscreen={onOpenFullscreen} />
                  : null
            ))}
          </div>
        )}
      </div>
      </InspectorSection>
    </div>
  );
}
