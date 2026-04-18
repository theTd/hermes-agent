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
        <button key={view} type="button" onClick={() => onSelect(view)}>
          <Badge variant={(currentView === view ? "success" : "outline") as any}>
            {view}
          </Badge>
        </button>
      ))}
    </div>
  );
}
