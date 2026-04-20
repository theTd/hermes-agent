import { StreamChunkBlock } from "./StreamChunkBlock";
import { ToolCallRow } from "./ToolCallRow";
import type { LiveTraceGroup, StreamChunk } from "../types";

function latestChunkFor(group: LiveTraceGroup, kind: "thinking" | "response") {
  for (let index = group.streamChunks.length - 1; index >= 0; index -= 1) {
    const chunk = group.streamChunks[index];
    if (chunk?.kind === kind) {
      return chunk;
    }
  }
  return null;
}

function isStreamChunk(chunk: StreamChunk | null): chunk is StreamChunk {
  return chunk !== null;
}

export function TraceEventList({ group }: { group: LiveTraceGroup }) {
  const recentChunks = [
    latestChunkFor(group, "thinking"),
    latestChunkFor(group, "response"),
  ]
    .filter(isStreamChunk)
    .sort((left, right) => {
      if (left.ts !== right.ts) {
        return left.ts - right.ts;
      }
      return left.sequence - right.sequence;
    });
  const recentTools = group.toolCalls.slice(-2);

  return (
    <div className="space-y-3">
      {group.stages.map((stage) => (
        <div key={stage.key} className="border-l border-border/40 pl-3">
          <div className="text-[10px] font-mono-ui uppercase tracking-widest opacity-50">{stage.title}</div>
          <div className="whitespace-pre-wrap break-all text-sm opacity-85 [overflow-wrap:anywhere]">{stage.summary}</div>
        </div>
      ))}
      {recentChunks.length > 0 && (
        <div className="space-y-2">
          {recentChunks.map((chunk) => (
            <StreamChunkBlock key={`${chunk.kind}:${chunk.ts}:${chunk.sequence}`} chunk={chunk} />
          ))}
        </div>
      )}
      {recentTools.length > 0 && (
        <div className="space-y-2">
          {recentTools.map((tool) => (
            <ToolCallRow key={tool.id} item={tool} />
          ))}
        </div>
      )}
    </div>
  );
}
