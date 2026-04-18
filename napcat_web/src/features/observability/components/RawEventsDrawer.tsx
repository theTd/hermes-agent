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

export function RawEventsDrawer({
  events,
  controller,
}: {
  events: NapcatEvent[];
  controller: ObservabilityController;
}) {
  const visibleEvents = events.slice(-50);

  return (
    <div className="min-w-0 space-y-2">
      {visibleEvents.map((event) => {
        const expanded = Boolean(controller.state.ui.expandedRawEventIds[event.event_id]);
        const compactPayload = stringifyPayload(event.payload);
        const expandedPayload = expanded ? stringifyPayload(event.payload, true) : compactPayload;
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
              {expanded ? expandedPayload : compactPayload.slice(0, 220)}
              {!expanded && compactPayload.length > 220 ? "…" : ""}
            </pre>
          </div>
        );
      })}
      {events.length > visibleEvents.length && (
        <p className="text-xs font-mono-ui opacity-40">
          Showing latest {visibleEvents.length} of {events.length} raw events.
        </p>
      )}
    </div>
  );
}
