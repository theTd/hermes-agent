function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };
  return (
    <button
      type="button"
      onClick={handleCopy}
      className="shrink-0 text-xs font-mono-ui opacity-70 hover:opacity-100"
    >
      {copied ? "copied" : "copy"}
    </button>
  );
}

import { useState } from "react";

export function TextBlockOverlay({
  block,
  title,
  onClose,
}: {
  block: { label: string; content: string; title?: string } | null;
  title?: string;
  onClose: () => void;
}) {
  if (!block) {
    return null;
  }

  return (
    <div
      className="fixed inset-0 z-50 bg-background/95 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget) {
          onClose();
        }
      }}
    >
      <div className="flex h-full min-w-0 flex-col p-4">
        <div className="flex items-start justify-between gap-4 border-b border-border/30 pb-3">
          <div className="min-w-0">
            <div className="text-[10px] font-mono-ui uppercase tracking-wider text-muted-foreground">
              {block.title ?? title ?? "Content"}
            </div>
            <div className="mt-1 break-all text-sm font-mono-ui [overflow-wrap:anywhere]">
              {block.label}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <CopyButton text={block.content} />
            <button
              type="button"
              onClick={onClose}
              className="text-xs font-mono-ui opacity-70 hover:opacity-100"
            >
              Close
            </button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-auto pt-4">
          <pre className="max-w-full whitespace-pre-wrap break-all rounded border border-border/30 bg-card/30 p-4 text-xs font-mono-ui text-foreground [overflow-wrap:anywhere]">
            {block.content}
          </pre>
        </div>
      </div>
    </div>
  );
}
