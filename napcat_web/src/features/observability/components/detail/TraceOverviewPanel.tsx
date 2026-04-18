import { Badge } from "@/components/ui/badge";
import { InspectorSection } from "../InspectorSection";
import type { TraceDetailViewModel } from "../../domain/detail-view";
import { LlmUsageSummaryCard } from "./LlmUsageSummaryCard";
import { OverviewStatGrid } from "./OverviewStatGrid";

function SecondaryOverviewDetails({
  entries,
}: {
  entries: Array<{ label: string; value: string }>;
}) {
  if (entries.length === 0) {
    return null;
  }

  return (
    <details
      className="rounded-xl border border-border/20 bg-background/20"
      data-overview-secondary="true"
    >
      <summary className="cursor-pointer list-none px-2.5 py-2 text-[10px] font-compressed uppercase tracking-[0.28em] text-muted-foreground/80">
        More Trace Metadata
      </summary>
      <div className="grid grid-cols-1 gap-x-3 gap-y-2 border-t border-border/15 px-2.5 py-2 text-[11px] sm:grid-cols-2">
        {entries.map((entry) => (
          <div key={entry.label} className="min-w-0">
            <div className="text-[10px] font-compressed uppercase tracking-[0.24em] text-muted-foreground/65">
              {entry.label}
            </div>
            <div
              className="mt-0.5 break-all font-mono-ui leading-4 text-foreground/85 [overflow-wrap:anywhere]"
              title={entry.value || "—"}
            >
              {entry.value || "—"}
            </div>
          </div>
        ))}
      </div>
    </details>
  );
}

export function TraceOverviewPanel({ viewModel }: { viewModel: TraceDetailViewModel }) {
  const { group, primaryOverviewEntries, secondaryOverviewEntries, usageSummary } = viewModel;

  return (
    <>
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-collapse text-lg tracking-wide">Trace Inspector</h2>
          <Badge variant={(group.status === "error" ? "destructive" : group.status === "completed" ? "success" : group.status === "rejected" || group.status === "ignored" ? "warning" : "outline") as any}>
            {group.status}
          </Badge>
          {group.activeStream && <Badge variant={"outline" as any}>LIVE</Badge>}
        </div>
        <p className="mt-2 break-all text-sm opacity-80 [overflow-wrap:anywhere]">{group.headline}</p>
      </div>

      <InspectorSection title="Overview">
        <div className="space-y-2">
          <OverviewStatGrid entries={primaryOverviewEntries} />
          <SecondaryOverviewDetails entries={secondaryOverviewEntries} />
          {usageSummary.requestCount > 0 && (
            <LlmUsageSummaryCard summary={usageSummary} />
          )}
        </div>
      </InspectorSection>
    </>
  );
}
