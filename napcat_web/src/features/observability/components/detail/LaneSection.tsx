import { Badge } from "@/components/ui/badge";
import { InspectorSection } from "../InspectorSection";
import type { TraceDetailLaneViewModel } from "../../domain/detail-view";
import { RequestBlockCard } from "./RequestBlockCard";
import { TimelineEventCard } from "./TimelineEventCard";

function MainOrchestratorThinkingPanel({
  content,
  onOpenFullscreen,
}: {
  content: string;
  onOpenFullscreen: (block: { label: string; content: string }) => void;
}) {
  if (!content.trim()) {
    return null;
  }

  return (
    <div
      className="rounded-2xl border border-border/30 bg-card/20 p-3"
      data-main-orchestrator-thinking="true"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] font-compressed uppercase tracking-[0.25em] opacity-55">
            Main Orchestrator Thinking
          </div>
          <div className="mt-1 text-[10px] font-mono-ui opacity-50">
            always visible
          </div>
        </div>
        <button
          type="button"
          className="text-xs font-mono-ui opacity-70 transition-opacity hover:opacity-100"
          onClick={() => onOpenFullscreen({ label: "main_orchestrator.thinking", content })}
        >
          fullscreen
        </button>
      </div>
      <pre className="mt-3 max-w-full whitespace-pre-wrap break-all rounded-xl border border-border/20 bg-background/35 p-3 text-xs font-mono-ui [overflow-wrap:anywhere]">
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
  onOpenFullscreen: (block: { label: string; content: string }) => void;
  resetKey: string;
}) {
  return (
    <InspectorSection
      title={lane.title}
      forceExpanded={lane.key === "main_orchestrator"}
      resetKey={resetKey}
    >
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={(lane.status === "error" ? "destructive" : lane.status === "completed" ? "success" : lane.status === "ignored" ? "warning" : "outline") as any}>
            {lane.status}
          </Badge>
          {lane.model && <Badge variant={"outline" as any}>{lane.model}</Badge>}
          {lane.provider && <Badge variant={"outline" as any}>{lane.provider}</Badge>}
          <span className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">
            {lane.requestCount} request{lane.requestCount === 1 ? "" : "s"}
          </span>
        </div>

        <MainOrchestratorThinkingPanel
          content={lane.mainOrchestratorThinking}
          onOpenFullscreen={onOpenFullscreen}
        />

        {lane.timelineItems.length === 0 ? (
          <p className="text-xs font-mono-ui opacity-40">No timeline entries for this lane.</p>
        ) : (
          <div className="space-y-3">
            {lane.timelineItems.map((item) => (
              item.kind === "request" && item.block
                ? (
                  <RequestBlockCard
                    key={item.id}
                    block={item.block}
                    onOpenFullscreen={onOpenFullscreen}
                    hideThinkingPreview={lane.key === "main_orchestrator" && Boolean(lane.mainOrchestratorThinking.trim())}
                    hideThinkingContent={lane.key === "main_orchestrator" && Boolean(lane.mainOrchestratorThinking.trim())}
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
  );
}
