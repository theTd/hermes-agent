export function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString();
}

export function fmtDuration(durationMs: number | null): string {
  if (durationMs == null) return "live";
  return durationMs < 1000 ? `${durationMs.toFixed(0)}ms` : `${(durationMs / 1000).toFixed(1)}s`;
}

export function formatPayloadPreview(value: unknown, maxChars: number = 1600): string {
  void maxChars;
  try {
    if (typeof value === "string") {
      return value.trim();
    }
    return JSON.stringify(value, null, 2) ?? "null";
  } catch {
    return "[payload preview unavailable]";
  }
}

export function previewText(value: string, maxChars: number = 220): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (normalized.length <= maxChars) {
    return normalized;
  }
  if (looksLikeStructuredPayloadText(normalized)) {
    return normalized;
  }
  return `${normalized.slice(0, maxChars)}…`;
}

export type ExpandableTextPanelVisibility = "preview" | "hidden";

function parseStructuredPayloadCandidate(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    return trimmed;
  }

  const objectIndex = trimmed.indexOf("{");
  const arrayIndex = trimmed.indexOf("[");
  const starts = [objectIndex, arrayIndex].filter((index) => index >= 0);
  if (starts.length === 0) {
    return null;
  }

  const startIndex = Math.min(...starts);
  const prefix = trimmed.slice(0, startIndex).trim();
  if (!prefix) {
    return trimmed.slice(startIndex);
  }

  const lines = prefix.split("\n").map((line) => line.trim()).filter(Boolean);
  if (lines.length === 0) {
    return trimmed.slice(startIndex);
  }
  const prefixLooksLikeDebugLabel = lines.every((line) => (
    /^#+\s*[\w./:-]+$/i.test(line)
    || /^[\w./:-]+$/i.test(line)
  ));
  return prefixLooksLikeDebugLabel ? trimmed.slice(startIndex) : null;
}

export function looksLikeStructuredPayloadText(value: string): boolean {
  const candidate = parseStructuredPayloadCandidate(value);
  if (!candidate) {
    return false;
  }

  try {
    const parsed = JSON.parse(candidate);
    return Boolean(parsed) && typeof parsed === "object";
  } catch {
    return false;
  }
}

export function getExpandableTextPanelVisibility(options: {
  title: string;
  label: string;
  content: string;
}): ExpandableTextPanelVisibility {
  const normalizedTitle = options.title.trim().toLowerCase();
  if (normalizedTitle === "thinking" || normalizedTitle === "reasoning" || normalizedTitle === "response" || normalizedTitle === "request body") {
    return "preview";
  }
  if (normalizedTitle === "debug payload") {
    return "hidden";
  }

  const normalizedLabel = options.label.trim().toLowerCase();
  if (
    normalizedLabel.includes("request_body")
    || normalizedLabel.includes("memory_prefetch_params_by_provider")
  ) {
    return "hidden";
  }

  return looksLikeStructuredPayloadText(options.content) ? "hidden" : "preview";
}
