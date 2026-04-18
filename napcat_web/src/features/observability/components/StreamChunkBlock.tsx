import type { StreamChunk } from "../types";

export function StreamChunkBlock({ chunk }: { chunk: StreamChunk }) {
  return (
    <div className="min-w-0 rounded border border-border/30 bg-card/30 p-2">
      <div className="mb-1 text-[10px] font-compressed uppercase tracking-widest opacity-50">
        {chunk.kind === "thinking" ? "Thinking" : "Response"} · #{chunk.sequence}
      </div>
      <pre className="max-w-full whitespace-pre-wrap break-all text-xs font-mono-ui opacity-85 [overflow-wrap:anywhere]">
        {chunk.content}
      </pre>
    </div>
  );
}
