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
  const latestStep = [...block.thinkingSteps].reverse().find((step) => step.text.trim());
  return latestStep?.text.trim() || block.thinkingFullText.trim() || block.thinkingText.trim();
}

function RequestPrimaryStats({ block }: { block: LlmRequestBlock }) {
  const primaryStats = [
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
    <div className="mt-3 flex flex-wrap gap-1.5 text-[10px] uppercase tracking-wider opacity-75" data-request-primary-stats="true">
      {primaryStats.map((entry) => (
        <div
          key={entry.label}
          className="rounded-lg border border-border/20 bg-background/25 px-2 py-1 font-mono-ui"
          data-request-stat={entry.label}
        >
          {entry.label}={entry.value}
        </div>
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
      ? { label: "thinking", value: previewText(latestThinkingPreview(block), 140) }
      : null,
  ].filter(Boolean) as Array<{ label: string; value: string }>;

  if (previewEntries.length === 0) {
    return null;
  }

  return (
    <div className="mt-3 space-y-2" data-request-preview-summary="true">
      {previewEntries.map((entry) => (
        <div
          key={entry.label}
          className="rounded-xl border border-border/20 bg-background/20 px-2.5 py-2"
          data-request-preview={entry.label}
        >
          <div className="text-[10px] font-compressed uppercase tracking-[0.24em] text-muted-foreground/75">
            {entry.label}
          </div>
          <div className="mt-1 text-xs leading-5 text-foreground/85">{entry.value}</div>
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
    <details
      className="rounded-xl border border-border/20 bg-background/20"
      data-request-metadata="true"
    >
      <summary className="cursor-pointer list-none px-2.5 py-2 text-[10px] font-compressed uppercase tracking-[0.28em] text-muted-foreground/80">
        More Request Metadata
      </summary>
      <div className="grid grid-cols-1 gap-x-3 gap-y-2 border-t border-border/15 px-2.5 py-2 text-[11px] sm:grid-cols-2">
        {metadataEntries.map((entry) => (
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
      {secondaryUsageEntries.length > 0 && (
        <div className="flex flex-wrap gap-1.5 border-t border-border/15 px-2.5 py-2 text-[10px] uppercase tracking-wider opacity-70">
          {secondaryUsageEntries.map((entry) => (
            <div
              key={entry.label}
              className="rounded-lg border border-border/20 bg-background/25 px-2 py-1 font-mono-ui"
              data-request-secondary-stat={entry.label}
            >
              {entry.label}={entry.value}
            </div>
          ))}
        </div>
      )}
    </details>
  );
}

function ThinkingStepsPanel({
  block,
  onOpenFullscreen,
}: {
  block: LlmRequestBlock;
  onOpenFullscreen: (block: { label: string; content: string }) => void;
}) {
  const fullThinking = block.thinkingFullText || block.thinkingText;
  const thinkingSteps = block.thinkingSteps.filter((step) => step.text.trim());
  if (!fullThinking.trim()) {
    return null;
  }

  if (thinkingSteps.length === 0) {
    return (
      <ExpandableTextPanel
        title="Thinking"
        label={`${block.id}.thinking`}
        content={fullThinking}
        visibility={getExpandableTextPanelVisibility({
          title: "Thinking",
          label: `${block.id}.thinking`,
          content: fullThinking,
        })}
        onOpenFullscreen={onOpenFullscreen}
      />
    );
  }

  return (
    <div
      className="rounded-2xl border border-border/30 bg-card/25 p-3"
      data-thinking-steps="true"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] font-compressed uppercase tracking-[0.25em] opacity-50">
            Thinking · {thinkingSteps.length} step{thinkingSteps.length === 1 ? "" : "s"}
          </div>
          <div className="mt-1 break-all text-[10px] font-mono-ui opacity-50 [overflow-wrap:anywhere]">
            {block.id}.thinking
          </div>
        </div>
        <button
          type="button"
          className="text-xs font-mono-ui opacity-70"
          onClick={() => onOpenFullscreen({ label: `${block.id}.thinking`, content: fullThinking })}
        >
          fullscreen
        </button>
      </div>
      <div className="mt-3 space-y-2.5">
        {thinkingSteps.map((step, index) => (
          <div
            key={`${block.id}.thinking.${index + 1}`}
            className="rounded-xl border border-border/20 bg-background/35 p-3"
            data-thinking-step={index + 1}
          >
            <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] font-mono-ui uppercase tracking-wider opacity-60">
              <span>Step {index + 1}</span>
              <span>{fmtTime(step.ts)}</span>
            </div>
            <pre className="mt-2 max-w-full whitespace-pre-wrap break-all text-xs font-mono-ui [overflow-wrap:anywhere]">
              {step.text}
            </pre>
          </div>
        ))}
      </div>
    </div>
  );
}

function RequestContentDetails({
  block,
  onOpenFullscreen,
  hideThinking = false,
}: {
  block: LlmRequestBlock;
  onOpenFullscreen: (block: { label: string; content: string }) => void;
  hideThinking?: boolean;
}) {
  const activityEvents = [...block.memoryEvents, ...block.skillEvents, ...block.policyEvents];
  const visibleSectionCount = [
    block.requestBody || block.requestPreview,
    hideThinking ? "" : (block.thinkingFullText || block.thinkingText),
    block.responseText,
  ].filter((item) => item.trim()).length;
  const toolLabel = block.toolCalls.length > 0 ? ` · ${block.toolCalls.length} tool${block.toolCalls.length === 1 ? "" : "s"}` : "";
  const activityLabel = activityEvents.length > 0 ? ` · ${activityEvents.length} activit${activityEvents.length === 1 ? "y" : "ies"}` : "";

  return (
    <details
      className="rounded-xl border border-border/20 bg-background/20"
      data-request-content="true"
      data-request-content-default-open={block.toolCalls.length > 0 ? "true" : "false"}
      open={block.toolCalls.length > 0}
    >
      <summary className="cursor-pointer list-none px-2.5 py-2 text-[10px] font-compressed uppercase tracking-[0.28em] text-muted-foreground/80">
        Full Request Content · {visibleSectionCount} section{visibleSectionCount === 1 ? "" : "s"}{toolLabel}{activityLabel}
      </summary>
      <div className="space-y-3 border-t border-border/15 px-2.5 py-2">
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
          <ThinkingStepsPanel
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
          <div className="space-y-2">
            <div className="text-[10px] font-compressed uppercase tracking-[0.25em] opacity-50">
              Tools
            </div>
            {block.toolCalls.map((tool) => (
              <ToolCallRow key={tool.id} item={tool} />
            ))}
          </div>
        )}

        {activityEvents.length > 0 && (
          <div className="space-y-2">
            <div className="text-[10px] font-compressed uppercase tracking-[0.25em] opacity-50">
              Request-local Activity
            </div>
            {activityEvents.map((event) => (
              <div key={event.event_id} className="rounded-xl border border-border/30 bg-background/30 p-3 text-xs">
                <div className="font-mono-ui opacity-80">{event.event_type}</div>
                <div className="mt-2 opacity-75">{previewText(formatPayloadPreview(event.payload), 220)}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    </details>
  );
}

export function RequestBlockCard({
  block,
  onOpenFullscreen,
  hideThinkingPreview = false,
  hideThinkingContent = false,
}: {
  block: LlmRequestBlock;
  onOpenFullscreen: (block: { label: string; content: string }) => void;
  hideThinkingPreview?: boolean;
  hideThinkingContent?: boolean;
}) {
  return (
    <div className="rounded-[1.2rem] border border-border/35 bg-card/28 p-3" data-request-card="true">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={(block.status === "failed" ? "destructive" : block.status === "completed" ? "success" : "outline") as any}>
          {block.status}
        </Badge>
        <Badge variant={"outline" as any}>{block.correlationMode}</Badge>
        {block.model && <Badge variant={"outline" as any}>{block.model}</Badge>}
        {block.provider && <Badge variant={"outline" as any}>{block.provider}</Badge>}
      </div>
      <div className="mt-3">
        <RequestPrimaryStats block={block} />
        {!hideThinkingPreview ? <RequestPreviewSummary block={block} /> : (
          <RequestPreviewSummary
            block={{ ...block, thinkingText: "", thinkingFullText: "", thinkingSteps: [], thinkingStepCount: 0 }}
          />
        )}
        <div className="mt-3 space-y-2">
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
