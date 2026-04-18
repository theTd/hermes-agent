import { useEffect, useState } from "react";
import { InspectorSection } from "./InspectorSection";
import { ExpandableTextPanel } from "./detail/ExpandableTextPanel";
import { getExpandableTextPanelVisibility } from "./detail/format";
import { LaneSection } from "./detail/LaneSection";
import { RawEventsPanel } from "./detail/RawEventsPanel";
import { TextBlockOverlay } from "./detail/TextBlockOverlay";
import { TraceOverviewPanel } from "./detail/TraceOverviewPanel";
import { buildTraceDetailViewModel } from "../domain/detail-view";
import type { ObservabilityController } from "../types";

export function TraceDetailView({ controller }: { controller: ObservabilityController }) {
  const group = controller.selectedGroup;
  const [showRawEvents, setShowRawEvents] = useState(false);
  const [activeTextBlock, setActiveTextBlock] = useState<{ label: string; content: string } | null>(null);

  useEffect(() => {
    if (!activeTextBlock) {
      return;
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setActiveTextBlock(null);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [activeTextBlock]);

  if (!group) {
    return (
      <div className="h-full min-w-0 overflow-y-auto overflow-x-hidden p-4">
        <p className="text-sm font-mono-ui opacity-40">Select a trace to inspect.</p>
      </div>
    );
  }

  const viewModel = buildTraceDetailViewModel(group);

  return (
    <>
      <div className="h-full min-w-0 space-y-3 overflow-y-auto overflow-x-hidden p-4">
        <TraceOverviewPanel viewModel={viewModel} />

        {viewModel.lanes.map((lane) => (
          <LaneSection
            key={`${group.traceId}:${lane.key}`}
            lane={lane}
            resetKey={group.traceId}
            onOpenFullscreen={setActiveTextBlock}
          />
        ))}

        {viewModel.rawContextBlocks.length > 0 && (
          <InspectorSection title="Context Debug">
            <div className="space-y-3">
              {viewModel.rawContextBlocks.map((block) => (
                <ExpandableTextPanel
                  key={block.label}
                  title="Captured Context"
                  label={block.label}
                  content={block.content}
                  visibility={getExpandableTextPanelVisibility({
                    title: "Captured Context",
                    label: block.label,
                    content: block.content,
                  })}
                  onOpenFullscreen={setActiveTextBlock}
                />
              ))}
            </div>
          </InspectorSection>
        )}

        <RawEventsPanel
          events={group.rawEvents}
          controller={controller}
          showRawEvents={showRawEvents}
          onToggle={() => setShowRawEvents((value) => !value)}
        />
      </div>

      <TextBlockOverlay
        block={activeTextBlock}
        title="Context / Prompt"
        onClose={() => setActiveTextBlock(null)}
      />
    </>
  );
}
