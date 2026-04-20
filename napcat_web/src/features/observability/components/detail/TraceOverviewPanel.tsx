import { Badge } from "@/components/ui/badge";
import { InspectorSection } from "../InspectorSection";
import type { TraceDetailViewModel } from "../../domain/detail-view";
import { LlmUsageSummaryCard } from "./LlmUsageSummaryCard";
import { OverviewStatGrid } from "./OverviewStatGrid";
import { fmtDuration } from "./format";

function SecondaryOverviewDetails({
  entries,
}: {
  entries: Array<{ label: string; value: string }>;
}) {
  if (entries.length === 0) {
    return null;
  }

  return (
    <div className="rounded bg-background/20 p-2" data-overview-secondary="true">
      <div className="grid grid-cols-1 gap-x-3 gap-y-1 text-[11px] sm:grid-cols-2">
        {entries.map((entry) => (
          <div key={entry.label} className="min-w-0">
            <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/60">
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
    </div>
  );
}

function TimingBreakdownCard({ viewModel }: { viewModel: TraceDetailViewModel["timingSummary"] }) {
  if (!viewModel) {
    return null;
  }

  const summaryEntries = [
    viewModel.endToEndMs != null ? { label: "end_to_end", value: fmtDuration(viewModel.endToEndMs) } : null,
    viewModel.measuredMs != null ? { label: "measured", value: fmtDuration(viewModel.measuredMs) } : null,
    viewModel.unaccountedMs != null ? { label: "unaccounted", value: fmtDuration(viewModel.unaccountedMs) } : null,
  ].filter((entry): entry is { label: string; value: string } => entry != null);

  if (summaryEntries.length === 0 && viewModel.items.length === 0) {
    return null;
  }

  return (
    <div className="rounded bg-background/20 p-2">
      <div className="flex flex-wrap items-center gap-2 text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/70">
        <span>Timing Breakdown</span>
        {summaryEntries.map((entry) => (
          <span key={entry.label} className="rounded bg-background/30 px-1.5 py-0.5 text-foreground/85">
            {entry.label}={entry.value}
          </span>
        ))}
      </div>
      {viewModel.items.length > 0 && (
        <div className="mt-2 space-y-1">
          {viewModel.items.map((item, index) => (
            <div key={`${item.key}-${index}`} className="rounded bg-background/20 px-2 py-1.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-foreground/90">{item.label}</div>
                  <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/60">
                    {item.sourceEventType || item.key}
                    {item.note ? ` · ${item.note}` : ""}
                  </div>
                </div>
                <div className="text-sm font-mono-ui text-foreground/90">
                  {fmtDuration(item.durationMs)}
                  {item.shareOfTotal != null ? ` · ${(item.shareOfTotal * 100).toFixed(1)}%` : ""}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function TraceOverviewPanel({ viewModel }: { viewModel: TraceDetailViewModel }) {
  const { group, primaryOverviewEntries, secondaryOverviewEntries, usageSummary, usageBreakdowns, timingSummary } = viewModel;

  return (
    <>
      <div className="flex items-center gap-2 flex-wrap">
        <Badge
          variant={(group.status === "error" ? "destructive" : group.status === "completed" ? "success" : group.status === "rejected" || group.status === "ignored" ? "warning" : "outline") as any}
          className="text-[10px] px-1.5 py-0 h-4"
        >
          {group.status}
        </Badge>
        {group.activeStream && (
          <Badge variant={"outline" as any} className="text-[10px] px-1.5 py-0 h-4">LIVE</Badge>
        )}
        <span className="text-sm opacity-80 truncate">{group.headline}</span>
        <span className="ml-auto text-[10px] font-mono-ui opacity-50">
          {fmtDuration(group.durationMs ?? 0)} · {group.eventCount} events
        </span>
      </div>

      <OverviewStatGrid entries={primaryOverviewEntries} />

      <InspectorSection title="Details" collapsible defaultExpanded={false}>
        <div className="space-y-2">
          <SecondaryOverviewDetails entries={secondaryOverviewEntries} />
          <TimingBreakdownCard viewModel={timingSummary} />
          {usageSummary.requestCount > 0 && (
            <LlmUsageSummaryCard summary={usageSummary} breakdowns={usageBreakdowns} />
          )}
        </div>
      </InspectorSection>
    </>
  );
}
