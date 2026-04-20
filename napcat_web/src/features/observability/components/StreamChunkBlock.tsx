import type { StreamChunk } from "../types";

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString();
}

export function StreamChunkBlock({ chunk }: { chunk: StreamChunk }) {
  return (
    <div className="min-w-0 rounded bg-background/20 p-2">
      <div className="mb-1 flex items-center gap-2 text-[10px] font-mono-ui uppercase tracking-wider opacity-50">
        <span>{chunk.kind === "thinking" ? "Thinking" : "Response"} · #{chunk.sequence}</span>
        <span className="normal-case tracking-normal opacity-60">{fmtTime(chunk.ts)}</span>
      </div>
      <pre className="max-w-full whitespace-pre-wrap break-all text-xs font-mono-ui opacity-85 [overflow-wrap:anywhere]">
        {chunk.content}
      </pre>
    </div>
  );
}
