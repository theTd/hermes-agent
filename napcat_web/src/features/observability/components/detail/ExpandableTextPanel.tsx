import { useState } from "react";
import type { ExpandableTextPanelVisibility } from "./format";

export function ExpandableTextPanel({
  title,
  content,
  label,
  onOpenFullscreen,
  visibility = "preview",
}: {
  title: string;
  content: string;
  label: string;
  onOpenFullscreen: (block: { label: string; content: string }) => void;
  visibility?: ExpandableTextPanelVisibility;
}) {
  const [expanded, setExpanded] = useState(false);
  if (!content.trim()) {
    return null;
  }
  const showContent = visibility === "preview" || expanded;
  return (
    <div className="rounded-2xl border border-border/30 bg-card/25 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] font-compressed uppercase tracking-[0.25em] opacity-50">
            {title}
          </div>
          <div className="mt-1 break-all text-[10px] font-mono-ui opacity-50 [overflow-wrap:anywhere]">
            {label}
          </div>
        </div>
        <div className="flex items-center gap-3 text-xs font-mono-ui opacity-70">
          <button type="button" onClick={() => setExpanded((value) => !value)}>
            {expanded ? "collapse" : "expand"}
          </button>
          <button
            type="button"
            onClick={() => onOpenFullscreen({ label, content })}
          >
            fullscreen
          </button>
        </div>
      </div>
      {showContent ? (
        <pre
          className={`mt-3 max-w-full whitespace-pre-wrap break-all rounded-xl border border-border/20 bg-background/40 p-3 text-xs font-mono-ui [overflow-wrap:anywhere] ${
            expanded ? "" : "max-h-40 overflow-hidden"
          }`}
        >
          {content}
        </pre>
      ) : null}
    </div>
  );
}
