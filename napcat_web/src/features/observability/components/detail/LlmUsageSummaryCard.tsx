import type { LlmUsageCostSummary } from "../../domain/detail-view";
import { fmtDuration } from "./format";

export function LlmUsageSummaryCard({ summary }: { summary: LlmUsageCostSummary }) {
  return (
    <div className="rounded-xl border border-border/25 bg-card/20 p-2.5 text-[11px] font-mono-ui" data-llm-summary="true">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 opacity-80">
          <span className="text-[10px] font-compressed uppercase tracking-[0.28em] opacity-60">
            LLM Total
          </span>
          <span>requests={summary.requestCount}</span>
          <span>calls={summary.apiCalls}</span>
          {summary.durationMs > 0 && <span>api_time={fmtDuration(summary.durationMs)}</span>}
        </div>
        <div className="rounded-lg border border-border/20 bg-background/30 px-2 py-1 text-[11px] text-right">
          <span className="text-[10px] uppercase tracking-wider opacity-55">cost </span>
          <span className="opacity-90">{summary.costLabel}</span>
        </div>
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5 text-[10px] uppercase tracking-wider opacity-70">
        <div className="rounded-lg border border-border/20 bg-background/25 px-2 py-1">prompt={summary.promptTokens.toLocaleString()}</div>
        <div className="rounded-lg border border-border/20 bg-background/25 px-2 py-1">input={summary.inputTokens.toLocaleString()}</div>
        <div className="rounded-lg border border-border/20 bg-background/25 px-2 py-1">output={summary.outputTokens.toLocaleString()}</div>
        <div className="rounded-lg border border-border/20 bg-background/25 px-2 py-1">cache_read={summary.cacheReadTokens.toLocaleString()}</div>
        {summary.cacheWriteTokens > 0 && (
          <div className="rounded-lg border border-border/20 bg-background/25 px-2 py-1">
            cache_write={summary.cacheWriteTokens.toLocaleString()}
          </div>
        )}
        {summary.reasoningTokens > 0 && (
          <div className="rounded-lg border border-border/20 bg-background/25 px-2 py-1">
            reasoning={summary.reasoningTokens.toLocaleString()}
          </div>
        )}
        <div className="rounded-lg border border-border/20 bg-background/25 px-2 py-1">total={summary.totalTokens.toLocaleString()}</div>
      </div>
    </div>
  );
}
