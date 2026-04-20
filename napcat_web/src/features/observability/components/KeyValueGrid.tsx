export function KeyValueGrid({ entries }: { entries: Array<{ label: string; value: string }> }) {
  return (
    <div className="min-w-0 grid grid-cols-[minmax(0,9rem)_minmax(0,1fr)] gap-2 text-xs font-mono-ui">
      {entries.map((entry) => (
        <div key={entry.label} className="contents">
          <span className="self-start py-1 text-muted-foreground/90">{entry.label}</span>
          <span className="min-w-0 break-all rounded border border-border/20 bg-muted/20 px-2 py-1 text-foreground">
            {entry.value || "—"}
          </span>
        </div>
      ))}
    </div>
  );
}
