import type { LlmUsageCostBreakdown, LlmUsageCostSummary } from "../../domain/detail-view";
import { fmtDuration } from "./format";

function fmtPercent(value: number | null): string {
  if (value == null || !Number.isFinite(value)) {
    return "n/a";
  }
  return `${(value * 100).toFixed(1)}%`;
}

function UsageTokenChips({ summary }: { summary: LlmUsageCostSummary }) {
  return (
    <div className="mt-1 flex flex-wrap gap-1 text-[10px] uppercase tracking-wider opacity-70">
      <span className="rounded bg-background/30 px-1.5 py-0.5">prompt={summary.promptTokens.toLocaleString()}</span>
      <span className="rounded bg-background/30 px-1.5 py-0.5">input={summary.inputTokens.toLocaleString()}</span>
      <span className="rounded bg-background/30 px-1.5 py-0.5">output={summary.outputTokens.toLocaleString()}</span>
      <span className="rounded bg-background/30 px-1.5 py-0.5">cache_read={summary.cacheReadTokens.toLocaleString()}</span>
      <span className="rounded bg-background/30 px-1.5 py-0.5">cache_hit_rate={fmtPercent(summary.cacheHitRate)}</span>
      {summary.cacheWriteTokens > 0 && (
        <span className="rounded bg-background/30 px-1.5 py-0.5">
          cache_write={summary.cacheWriteTokens.toLocaleString()}
        </span>
      )}
      {summary.reasoningTokens > 0 && (
        <span className="rounded bg-background/30 px-1.5 py-0.5">
          reasoning={summary.reasoningTokens.toLocaleString()}
        </span>
      )}
      <span className="rounded bg-background/30 px-1.5 py-0.5">total={summary.totalTokens.toLocaleString()}</span>
    </div>
  );
}

function UsageSummaryBlock({
  title,
  summary,
  breakdown = false,
}: {
  title: string;
  summary: LlmUsageCostSummary;
  breakdown?: boolean;
}) {
  return (
    <div
      className={`rounded bg-background/20 p-2 text-[11px] font-mono-ui ${breakdown ? "bg-background/15" : ""}`}
      data-llm-summary={breakdown ? "breakdown" : "total"}
      data-llm-summary-key={summary.key}
    >
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 opacity-80">
          <span className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">
            {title}
          </span>
          <span>requests={summary.requestCount}</span>
          <span>calls={summary.apiCalls}</span>
          {summary.durationMs > 0 && <span>api_time={fmtDuration(summary.durationMs)}</span>}
        </div>
        <div className="rounded bg-background/30 px-2 py-0.5 text-[11px] text-right">
          <span className="text-[10px] uppercase tracking-wider opacity-55">cost </span>
          <span className="opacity-90">{summary.costLabel}</span>
        </div>
      </div>
      <UsageTokenChips summary={summary} />
    </div>
  );
}

export function LlmUsageSummaryCard({
  summary,
  breakdowns,
}: {
  summary: LlmUsageCostSummary;
  breakdowns: LlmUsageCostBreakdown[];
}) {
  return (
    <div className="space-y-1.5" data-llm-summary-root="true">
      <UsageSummaryBlock title="LLM Total" summary={summary} />
      {breakdowns.length > 0 && (
        <div className="space-y-1.5" data-llm-breakdowns="true">
          {breakdowns.map((breakdown) => (
            <UsageSummaryBlock
              key={breakdown.key}
              title={breakdown.label}
              summary={breakdown.summary}
              breakdown
            />
          ))}
        </div>
      )}
    </div>
  );
}
