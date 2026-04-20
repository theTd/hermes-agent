import { Badge } from "@/components/ui/badge";
import { ToolCallRow } from "../ToolCallRow";
import type { LlmRequestBlock } from "../../types";
import { ExpandableTextPanel } from "./ExpandableTextPanel";
import {
  fmtDuration,
  fmtTime,
  formatPayloadPreview,
  getExpandableTextPanelVisibility,
  previewText,
} from "./format";

function latestThinkingPreview(block: LlmRequestBlock): string {
  return block.thinkingFullText.trim() || block.thinkingText.trim();
}

function RequestPrimaryStats({ block }: { block: LlmRequestBlock }) {
  const primaryStats = [
    block.ttftMs != null ? { label: "ttft", value: fmtDuration(block.ttftMs) } : null,
    block.durationMs != null ? { label: "duration", value: fmtDuration(block.durationMs) } : null,
    block.usage.totalTokens > 0 ? { label: "total", value: block.usage.totalTokens.toLocaleString() } : null,
    block.usage.apiCalls > 0 ? { label: "calls", value: String(block.usage.apiCalls) } : null,
    block.usage.inputTokens > 0 ? { label: "input", value: block.usage.inputTokens.toLocaleString() } : null,
    block.usage.outputTokens > 0 ? { label: "output", value: block.usage.outputTokens.toLocaleString() } : null,
  ].filter(Boolean) as Array<{ label: string; value: string }>;

  if (primaryStats.length === 0) {
    return null;
  }

  return (
    <div className="mt-1 flex flex-wrap gap-1 text-[10px] uppercase tracking-wider opacity-70" data-request-primary-stats="true">
      {primaryStats.map((entry) => (
        <span
          key={entry.label}
          className="rounded bg-background/30 px-1.5 py-0.5 font-mono-ui"
          data-request-stat={entry.label}
        >
          {entry.label}={entry.value}
        </span>
      ))}
    </div>
  );
}

function RequestPreviewSummary({ block }: { block: LlmRequestBlock }) {
  const previewEntries = [
    block.responseText.trim()
      ? { label: "response", value: previewText(block.responseText, 180) }
      : null,
    latestThinkingPreview(block)
      ? { label: "reasoning", value: previewText(latestThinkingPreview(block), 140) }
      : null,
  ].filter(Boolean) as Array<{ label: string; value: string }>;

  if (previewEntries.length === 0) {
    return null;
  }

  return (
    <div className="mt-1 space-y-1" data-request-preview-summary="true">
      {previewEntries.map((entry) => (
        <div
          key={entry.label}
          className="rounded bg-background/20 px-2 py-1"
          data-request-preview={entry.label}
        >
          <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/70">
            {entry.label}
          </div>
          <div className="mt-0.5 text-xs leading-4 text-foreground/85">{entry.value}</div>
        </div>
      ))}
    </div>
  );
}

function RequestMetadataDetails({ block }: { block: LlmRequestBlock }) {
  const metadataEntries = [
    { label: "scope", value: block.agentScope || "—" },
    { label: "child_id", value: block.childId || "—" },
    { label: "started", value: fmtTime(block.startedAt) },
    { label: "ended", value: block.endedAt ? fmtTime(block.endedAt) : "live" },
    { label: "request_id", value: block.id },
    { label: "correlation", value: block.correlationMode },
  ];
  const secondaryUsageEntries = [
    block.usage.promptTokens > 0 ? { label: "prompt", value: block.usage.promptTokens.toLocaleString() } : null,
    block.usage.cacheReadTokens > 0 ? { label: "cache_read", value: block.usage.cacheReadTokens.toLocaleString() } : null,
    block.usage.cacheWriteTokens > 0 ? { label: "cache_write", value: block.usage.cacheWriteTokens.toLocaleString() } : null,
    block.usage.reasoningTokens > 0 ? { label: "reasoning", value: block.usage.reasoningTokens.toLocaleString() } : null,
  ].filter(Boolean) as Array<{ label: string; value: string }>;

  return (
    <div className="mt-2 rounded bg-background/20 p-2" data-request-metadata="true">
      <div className="grid grid-cols-1 gap-x-3 gap-y-1 text-[11px] sm:grid-cols-2">
        {metadataEntries.map((entry) => (
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
      {secondaryUsageEntries.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1 border-t border-border/10 pt-2 text-[10px] uppercase tracking-wider opacity-60">
          {secondaryUsageEntries.map((entry) => (
            <span
              key={entry.label}
              className="rounded bg-background/30 px-1.5 py-0.5 font-mono-ui"
              data-request-secondary-stat={entry.label}
            >
              {entry.label}={entry.value}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function ThinkingBlockPanel({
  block,
  onOpenFullscreen,
}: {
  block: LlmRequestBlock;
  onOpenFullscreen: (block: { label: string; content: string; title?: string }) => void;
}) {
  const fullThinking = block.thinkingFullText || block.thinkingText;
  if (!fullThinking.trim()) {
    return null;
  }

  return (
    <div data-thinking-block="true">
      <ExpandableTextPanel
        title="Reasoning"
        label={`${block.id}.reasoning`}
        content={fullThinking}
        visibility={getExpandableTextPanelVisibility({
          title: "Reasoning",
          label: `${block.id}.reasoning`,
          content: fullThinking,
        })}
        onOpenFullscreen={onOpenFullscreen}
      />
    </div>
  );
}

function RequestContentDetails({
  block,
  onOpenFullscreen,
  hideThinking = false,
}: {
  block: LlmRequestBlock;
  onOpenFullscreen: (block: { label: string; content: string; title?: string }) => void;
  hideThinking?: boolean;
}) {
  const activityEvents = [...block.memoryEvents, ...block.skillEvents, ...block.policyEvents];
  const toolLabel = block.toolCalls.length > 0 ? ` · ${block.toolCalls.length} tool${block.toolCalls.length === 1 ? "" : "s"}` : "";
  const activityLabel = activityEvents.length > 0 ? ` · ${activityEvents.length} activit${activityEvents.length === 1 ? "y" : "ies"}` : "";

  return (
    <div className="mt-2 space-y-2 rounded bg-background/20 p-2" data-request-content="true">
      <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground/60">
        Full Request Content{toolLabel}{activityLabel}
      </div>
      <ExpandableTextPanel
        title="Request Body"
        label={`${block.id}.request`}
        content={block.requestBody || block.requestPreview}
        visibility={getExpandableTextPanelVisibility({
          title: "Request Body",
          label: `${block.id}.request`,
          content: block.requestBody || block.requestPreview,
        })}
        onOpenFullscreen={onOpenFullscreen}
      />
      {hideThinking ? null : (
        <ThinkingBlockPanel
          block={block}
          onOpenFullscreen={onOpenFullscreen}
        />
      )}
      <ExpandableTextPanel
        title="Response"
        label={`${block.id}.response`}
        content={block.responseText}
        visibility={getExpandableTextPanelVisibility({
          title: "Response",
          label: `${block.id}.response`,
          content: block.responseText,
        })}
        onOpenFullscreen={onOpenFullscreen}
      />

      {block.toolCalls.length > 0 && (
        <div className="space-y-1">
          <div className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">
            Tools
          </div>
          {block.toolCalls.map((tool) => (
            <ToolCallRow key={tool.id} item={tool} />
          ))}
        </div>
      )}

      {activityEvents.length > 0 && (
        <div className="space-y-1">
          <div className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">
            Request-local Activity
          </div>
          {activityEvents.map((event) => (
            <div key={event.event_id} className="rounded border border-border/10 bg-background/30 px-2 py-1 text-xs">
              <div className="font-mono-ui opacity-80">{event.event_type}</div>
              <div className="mt-1 opacity-70">{previewText(formatPayloadPreview(event.payload), 220)}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function RequestBlockCard({
  block,
  onOpenFullscreen,
  hideThinkingPreview = false,
  hideThinkingContent = false,
  laneModel,
  laneProvider,
  laneRequestCount,
}: {
  block: LlmRequestBlock;
  onOpenFullscreen: (block: { label: string; content: string; title?: string }) => void;
  hideThinkingPreview?: boolean;
  hideThinkingContent?: boolean;
  laneModel?: string;
  laneProvider?: string;
  laneRequestCount?: number;
}) {
  const showModelBadge = block.model && !(laneRequestCount === 1 && laneModel === block.model);
  const showProviderBadge = block.provider && !(laneRequestCount === 1 && laneProvider === block.provider);

  return (
    <div className="border-b border-border/10 px-3 py-2 last:border-b-0" data-request-card="true">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant={(block.status === "failed" ? "destructive" : block.status === "completed" ? "success" : "outline") as any} className="text-[10px] px-1.5 py-0 h-4">
          {block.status}
        </Badge>
        {showModelBadge && <Badge variant={"outline" as any} className="text-[10px] px-1.5 py-0 h-4">{block.model}</Badge>}
        {showProviderBadge && <Badge variant={"outline" as any} className="text-[10px] px-1.5 py-0 h-4">{block.provider}</Badge>}
      </div>
      <div className="mt-1">
        <RequestPrimaryStats block={block} />
        {!hideThinkingPreview ? <RequestPreviewSummary block={block} /> : (
          <RequestPreviewSummary
            block={{ ...block, thinkingText: "", thinkingFullText: "", thinkingSteps: [], thinkingStepCount: 0 }}
          />
        )}
        <div className="mt-2 space-y-2">
          <RequestMetadataDetails block={block} />
          <RequestContentDetails
            block={block}
            onOpenFullscreen={onOpenFullscreen}
            hideThinking={hideThinkingContent}
          />
        </div>
      </div>
    </div>
  );
}
