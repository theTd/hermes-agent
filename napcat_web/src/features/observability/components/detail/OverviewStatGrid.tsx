export function OverviewStatGrid({
  entries,
}: {
  entries: Array<{ label: string; value: string }>;
}) {
  return (
    <div
      className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-4"
      data-overview-grid="true"
    >
      {entries.map((entry) => (
        <div
          key={entry.label}
          className="min-w-0 rounded-xl border border-border/25 bg-card/20 px-2.5 py-2"
          data-overview-stat={entry.label}
        >
          <div className="text-[10px] font-compressed uppercase tracking-[0.28em] text-muted-foreground/80">
            {entry.label}
          </div>
          <div
            className="mt-1 min-w-0 break-all text-[11px] font-mono-ui leading-4 text-foreground [overflow-wrap:anywhere]"
            title={entry.value || "—"}
          >
            {entry.value || "—"}
          </div>
        </div>
      ))}
    </div>
  );
}
