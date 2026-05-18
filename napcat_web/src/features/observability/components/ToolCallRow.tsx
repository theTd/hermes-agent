import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import type { ToolCallItem } from "../types";

export function ToolCallRow({ item }: { item: ToolCallItem }) {
  const [expanded, setExpanded] = useState(false);
  const hasLongContent =
    (item.argsPreview && item.argsPreview.length > 200) ||
    (item.resultPreview && item.resultPreview.length > 200);

  return (
    <div className="rounded border border-border/30 bg-card/30 p-2">
      <div className="flex items-center gap-2">
        <Badge variant={(item.status === "failed" ? "destructive" : item.status === "completed" ? "success" : "outline") as any}>
          {item.status}
        </Badge>
        <span className="text-xs font-mono-ui opacity-85">{item.toolName}</span>
        {item.durationMs != null && (
          <span className="text-[10px] font-mono-ui opacity-50">{item.durationMs.toFixed(0)}ms</span>
        )}
        {hasLongContent && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="ml-auto text-[10px] font-mono-ui opacity-60 hover:opacity-100"
          >
            {expanded ? "collapse" : "expand"}
          </button>
        )}
      </div>
      {item.argsPreview && (
        <pre className={`mt-1 whitespace-pre-wrap break-words text-xs font-mono-ui opacity-70 ${!expanded && hasLongContent ? "max-h-24 overflow-hidden" : ""}`}>
          {item.argsPreview}
        </pre>
      )}
      {item.resultPreview && (
        <pre className={`mt-1 whitespace-pre-wrap break-words text-xs font-mono-ui opacity-70 ${!expanded && hasLongContent ? "max-h-24 overflow-hidden" : ""}`}>
          {item.resultPreview}
        </pre>
      )}
      {item.error && <pre className="mt-1 whitespace-pre-wrap break-words text-xs font-mono-ui text-destructive">{item.error}</pre>}
    </div>
  );
}
