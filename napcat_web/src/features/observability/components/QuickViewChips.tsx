import { Badge } from "@/components/ui/badge";
import type { ObservabilityView } from "../types";

const VIEWS: ObservabilityView[] = [
  "all",
  "active",
  "errors",
  "rejected",
  "tools",
  "streaming",
  "alerts",
];

const VIEW_TOOLTIPS: Record<ObservabilityView, string> = {
  all: "Show all traces",
  active: "Traces currently in progress",
  errors: "Traces that ended with an error",
  rejected: "Traces that were rejected",
  tools: "Traces with tool calls",
  streaming: "Traces with active streams",
  alerts: "Traces with raised alerts",
};

export function QuickViewChips({
  currentView,
  onSelect,
}: {
  currentView: ObservabilityView;
  onSelect: (view: ObservabilityView) => void;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {VIEWS.map((view) => (
        <button key={view} type="button" onClick={() => onSelect(view)} title={VIEW_TOOLTIPS[view]}>
          <Badge variant={(currentView === view ? "success" : "outline") as any}>
            {view}
          </Badge>
        </button>
      ))}
    </div>
  );
}
