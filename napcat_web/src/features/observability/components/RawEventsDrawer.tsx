import { useMemo, useState } from "react";
import type { NapcatEvent } from "@/lib/napcat-api";
import type { ObservabilityController } from "../types";

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString();
}

function stringifyPayload(payload: unknown, pretty = false): string {
  try {
    return JSON.stringify(payload, null, pretty ? 2 : 0);
  } catch {
    return "[payload unavailable]";
  }
}

const PAGE_SIZE = 50;

export function RawEventsDrawer({
  events,
  controller,
}: {
  events: NapcatEvent[];
  controller: ObservabilityController;
}) {
  const [displayLimit, setDisplayLimit] = useState(PAGE_SIZE);
  const visibleEvents = events.slice(-displayLimit);

  const eventStrings = useMemo(() =>
    visibleEvents.map((event) => ({
      id: event.event_id,
      compact: stringifyPayload(event.payload),
      pretty: stringifyPayload(event.payload, true),
    })),
    [visibleEvents]
  );

  return (
    <div className="min-w-0 space-y-2">
      {visibleEvents.map((event, index) => {
        const expanded = Boolean(controller.state.ui.expandedRawEventIds[event.event_id]);
        const strings = eventStrings[index] ?? { compact: "", pretty: "" };
        return (
          <div key={event.event_id} className="rounded border border-border/30 bg-card/20 p-2">
            <button type="button" className="w-full text-left" onClick={() => controller.toggleRawEventExpanded(event.event_id)}>
              <div className="flex gap-2 text-xs font-mono-ui">
                <span className="opacity-50">{fmtTime(event.ts)}</span>
                <span className="opacity-80">{event.event_type}</span>
                <span className="opacity-40">{event.severity}</span>
              </div>
            </button>
            <pre className="mt-2 max-w-full whitespace-pre-wrap break-all text-xs font-mono-ui opacity-70 [overflow-wrap:anywhere]">
              {expanded ? strings.pretty : strings.compact}
            </pre>
          </div>
        );
      })}
      {events.length > visibleEvents.length && (
        <div className="flex items-center gap-3">
          <p className="text-xs font-mono-ui opacity-40">
            Showing latest {visibleEvents.length} of {events.length} raw events.
          </p>
          <button
            type="button"
            onClick={() => setDisplayLimit((prev) => prev + PAGE_SIZE)}
            className="text-xs font-mono-ui opacity-70 hover:opacity-100"
          >
            Load more
          </button>
        </div>
      )}
    </div>
  );
}
