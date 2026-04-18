import { Badge } from "@/components/ui/badge";
import type { ToolCallItem } from "../types";

export function ToolCallRow({ item }: { item: ToolCallItem }) {
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
      </div>
      {item.argsPreview && <pre className="mt-1 whitespace-pre-wrap break-words text-xs font-mono-ui opacity-70">{item.argsPreview}</pre>}
      {item.resultPreview && <pre className="mt-1 whitespace-pre-wrap break-words text-xs font-mono-ui opacity-70">{item.resultPreview}</pre>}
      {item.error && <pre className="mt-1 whitespace-pre-wrap break-words text-xs font-mono-ui text-destructive">{item.error}</pre>}
    </div>
  );
}
