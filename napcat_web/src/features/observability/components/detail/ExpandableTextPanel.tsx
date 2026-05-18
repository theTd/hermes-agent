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
  onOpenFullscreen: (block: { label: string; content: string; title?: string }) => void;
  visibility?: ExpandableTextPanelVisibility;
}) {
  const [expanded, setExpanded] = useState(false);
  if (!content.trim()) {
    return null;
  }
  const showContent = visibility === "preview" || expanded;
  return (
    <div className="rounded bg-background/20 p-2">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] font-mono-ui uppercase tracking-wider opacity-50">
            {title}
          </div>
          <div className="mt-0.5 break-all text-[10px] font-mono-ui opacity-50 [overflow-wrap:anywhere]">
            {label}
          </div>
        </div>
        <div className="flex items-center gap-2 text-xs font-mono-ui opacity-70">
          <button type="button" onClick={() => setExpanded((value) => !value)}>
            {expanded ? "collapse" : "expand"}
          </button>
          <button
            type="button"
            onClick={() => onOpenFullscreen({ label, content, title })}
          >
            fullscreen
          </button>
        </div>
      </div>
      {showContent ? (
        <div className={`relative mt-2 ${expanded ? "" : "max-h-40 overflow-hidden"}`}>
          <pre
            className="max-w-full whitespace-pre-wrap break-all rounded bg-background/30 p-2 text-xs font-mono-ui [overflow-wrap:anywhere]"
          >
            {content}
          </pre>
          {!expanded && (
            <div className="pointer-events-none absolute bottom-0 left-0 right-0 h-8 bg-gradient-to-t from-background/20 to-transparent" />
          )}
        </div>
      ) : null}
    </div>
  );
}
