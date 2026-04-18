import { RawEventsDrawer } from "../RawEventsDrawer";
import { InspectorSection } from "../InspectorSection";
import type { ObservabilityController } from "../../types";

export function RawEventsPanel({
  events,
  controller,
  showRawEvents,
  onToggle,
}: {
  events: Parameters<typeof RawEventsDrawer>[0]["events"];
  controller: ObservabilityController;
  showRawEvents: boolean;
  onToggle: () => void;
}) {
  return (
    <InspectorSection title={`Raw Events (${events.length})`}>
      <div className="space-y-3">
        <button
          type="button"
          onClick={onToggle}
          className="text-xs font-mono-ui opacity-70 hover:opacity-100"
        >
          {showRawEvents ? "Hide raw events" : "Show raw events"}
        </button>
        {showRawEvents ? (
          <RawEventsDrawer events={events} controller={controller} />
        ) : (
          <p className="text-xs font-mono-ui opacity-40">
            Raw event payloads are hidden by default for performance.
          </p>
        )}
      </div>
    </InspectorSection>
  );
}
